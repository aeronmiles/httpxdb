"""
This module provides classes for managing HTTP requests and rate limiting.
"""
from abc import ABC, abstractmethod
import asyncio
from copy import deepcopy
import time
from typing import Any, Coroutine, Dict, Generic, List, Optional, TypeVar
import httpx
from corex import logger
from ..db import DatabaseManager


RequestorType = TypeVar("RequestorType", bound="Requestor")
LimiterType = TypeVar("LimiterType", bound="RateLimitContext")
T = TypeVar("T")


class RateLimitContext:    
    def __init__(self, max_calls: int, period: int, max_concurrency: int = 1):
        self._max_calls = max_calls  # Period in seconds
        self._rate = max_calls / period  # Tokens added per second
        self._tokens = max_calls  # Maximum tokens
        self._last = time.monotonic()
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(max_concurrency)
    
    async def set_used_tokens(self, used: int):
        async with self._lock:
            self._tokens = self._max_calls - used
            
    async def sleep(self, seconds: float):
        async with self._lock:
            await asyncio.sleep(seconds)

    async def _acquire_token(self, weight: int = 1):
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last
            self._last = now
            self._tokens = min(self._tokens + (elapsed * self._rate), self._max_calls)
            required_tokens = weight
            if self._tokens < required_tokens:
                logger.debug(f"Sleeping for {(required_tokens - self._tokens) / self._rate} seconds")
                await asyncio.sleep((required_tokens - self._tokens) / self._rate)
                now = time.monotonic()
                elapsed = now - self._last
                self._last = now
                self._tokens = min(self._tokens + (elapsed * self._rate), self._max_calls)
            self._tokens -= required_tokens

    async def limit_request(self, weight: int = 1):
        async with self._semaphore:
            await self._acquire_token(weight)

    @abstractmethod
    async def adjust_rate_limit(self, headers: httpx.Headers):
        """
        Adjusts the rate limit based on the response headers from an API.
        Subclasses should implement this to handle specific API rate limiting schemes.
        """
        pass
    

class AsyncClient(ABC):
    def __init__(
        self,
        base_url: str,
        headers: dict,
        follow_redirects: bool = True,
        http2: bool = True,
        timeout: int = 30,
        rate_limit_context: LimiterType = None
    ):
        if base_url.endswith("/"):
            base_url = base_url[:-1]
        self.base_url = base_url
        self._headers = headers
        self._session = httpx.AsyncClient(headers=self._headers,
                                          follow_redirects=follow_redirects,
                                          http2=http2,
                                          timeout=timeout)
        self._rate_limit_context = rate_limit_context

    async def __aenter__(self):
        await self._session.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self._session.__aexit__(exc_type, exc, tb)
        
    async def _post_signed(self, signed_request: str, data: Any = None, **kwargs: Any) -> httpx.Response:
        response = await self._session.post(signed_request, data=data, **kwargs)
        return await self._handle(response)

    async def _delete_signed(self, signed_request: str, **kwargs: Any) -> httpx.Response:
        response = await self._session.delete(signed_request, **kwargs)
        return await self._handle(response)
    
    @abstractmethod
    async def get(self, endpoint: str, params: Optional[dict] = None):
        """
        """
        raise NotImplementedError("All subclasses must implement the get method")

    async def _get(self, endpoint: str, params: Optional[str] = None, **kwargs) -> httpx.Response:
        url = f"{self.base_url}{endpoint}"
        if params:
            url += f"?{params}"

        response = await self._session.get(url, **kwargs)
        await self._rate_limit_context.adjust_rate_limit(response.headers)
        await self._rate_limit_context.limit_request()
        return await self._handle(response)

    async def _post(self, endpoint: str, data: Any = None, **kwargs: Any) -> httpx.Response:
        url = f"{self.base_url}{endpoint}"
        response = await self._session.post(url, data=data, **kwargs)
        return await self._handle(response)

    async def _put(self, endpoint: str, data: Any = None, **kwargs: Any) -> httpx.Response:
        url = f"{self.base_url}{endpoint}"
        response = await self._session.put(url, data=data, **kwargs)
        return await self._handle(response)

    async def _delete(self, endpoint: str, **kwargs: Any) -> httpx.Response:
        url = f"{self.base_url}{endpoint}"
        response = await self._session.delete(url, **kwargs)
        return await self._handle(response)
    
    async def _handle(self, response: httpx.Response) -> httpx.Response:
        try:
            response.raise_for_status()
            return response

        except httpx.HTTPStatusError as e:
            # Raised when response status code is 400 or higher
            logger.error(f"HTTP httpx.Response Error: {e.response.status_code} {e.response.text}")

        except httpx.RequestError as e:
            # Raised in case of connection errors
            logger.error(f"Connection error occurred while handling the request: {e}")

        except Exception as e:
            # Catch any other exceptions that may occur during response handling
            logger.error(f"Unexpected error occurred while processing the response: {e}")

        return response
    

class IDataRequestor(ABC):
    @abstractmethod
    async def request(self, params: Dict[str, Any]):
        raise NotImplementedError("All subclasses must implement the request method")


class HTTPRequestor(Generic[T], IDataRequestor):
    def __init__(self, client: AsyncClient, endpoint: str, required_params: List[str]):
        self._client = client
        self._endpoint = endpoint if endpoint.startswith("/") else "/" + endpoint
        self.params = {}
        self._required_params = required_params

    def has_required_params(self) -> bool:
        missing_params = [p for p in self._required_params if p not in self.params]
        if missing_params:
            logger.warning(f"Missing required params: {missing_params}")
            return False
        return True

    @abstractmethod
    async def _request_func(self, params: Dict[str, Any]) -> Coroutine[Any, Any, Optional[T]]:
        raise NotImplementedError

    async def request(self) -> Optional[T]:
        if not self.has_required_params():
            return None
        return await self._request_func(self.params)


# @TODO: Change to DBRequestor and break out request method for composition
class Requestor(Generic[T], ABC):
    """
    This class provides methods for managing HTTP requests.
    """
    def __init__(
        self,
        dbm: DatabaseManager,
        client: AsyncClient,
        endpoint: str,
        required_params: List[str],
        default_return_value: Any,
        # request_weight: int
    ) -> None:
        self._dbm = dbm
        self._client = client
        if not endpoint.startswith("/"):
            endpoint = "/" + endpoint
        self._endpoint = endpoint
        self.params = {}
        self.__required_params = required_params
        self.__async_tasks: List[Coroutine] = []
        self._save: bool = True
        self._delete_from_db: bool = False
        self._default_return_value: T = default_return_value
        # self._request_weight: int = request_weight
        
    @property
    def namespace(self) -> str:
        return self._client.base_url + self._endpoint
    
    @abstractmethod
    async def _request_func(self, params: Dict) -> Coroutine[Any, Any, Optional[Any]]:
        raise NotImplementedError("All subclasses must implement the _request_func method")
    
    def __has_required_params(self) -> bool:
        for p in self.__required_params:
            if p not in self.params:
                logger.error(f"Missing required param: {p}")
                
        return all([p in self.params for p in self.__required_params])

    def save(self: RequestorType, save: bool) -> RequestorType:
        self._save = save
        return self

    def delete_from_db(self: RequestorType) -> RequestorType:
        if not self.__has_required_params():
            return self

        self.__async_tasks.append(self._dbm.delete_encoded(self.namespace, self.params))
        return self
    
    async def request_only(self) -> None:
        if await self._dbm.contains_encoded(self.namespace, self.params):
            return
        await self.request()

    async def request(self) -> T:
        """Request order book for symbol and save them to cache"""
        # logger.info(f"Requesting {self._endpoint} with params: {self.params}")
        if not self.__has_required_params():
            raise RuntimeError("Required params not set")

        if self.__async_tasks:
            for t in self.__async_tasks:
                await t

        resp = await self._dbm.fetch_encoded(
            self.namespace,
            self._request_func,
            self.params,
            self._save,
        )
        if not resp:
            return deepcopy(self._default_return_value)
        else:
            return resp
