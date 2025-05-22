import json
from collections.abc import AsyncGenerator
from typing import Any
from uuid import uuid4

import httpx
from httpx_sse import SSEError, aconnect_sse

from a2a.client.errors import A2AClientHTTPError, A2AClientJSONError
from a2a.types import (AgentCard, CancelTaskRequest, CancelTaskResponse,
                       GetTaskPushNotificationConfigRequest,
                       GetTaskPushNotificationConfigResponse, GetTaskRequest,
                       GetTaskResponse, SendMessageRequest,
                       SendMessageResponse, SendStreamingMessageRequest,
                       SendStreamingMessageResponse,
                       SetTaskPushNotificationConfigRequest,
                       SetTaskPushNotificationConfigResponse)
from a2a.utils.telemetry import SpanKind, trace_class


class A2ACardResolver:
    """Agent Card resolver."""

    def __init__(
        self,
        httpx_client: httpx.AsyncClient,
        base_url: str,
        agent_card_path: str = '/.well-known/agent.json',
        extended_agent_card_path: str = '/agent/authenticatedExtendedCard',
    ):
        """Initializes the A2ACardResolver.

        Args:
            httpx_client: An async HTTP client instance (e.g., httpx.AsyncClient).
            base_url: The base URL of the agent's host.
            agent_card_path: The path to the agent card endpoint, relative to the base URL.
        """
        self.base_url = base_url.rstrip('/')
        self.agent_card_path = agent_card_path.lstrip('/')
        self.extended_agent_card_path = extended_agent_card_path.lstrip('/')
        self.httpx_client = httpx_client

    async def get_agent_card(
        self, http_kwargs: dict[str, Any] | None = None
    ) -> AgentCard:
        # Fetch the initial public agent card
        public_card_url = f'{self.base_url}/{self.agent_card_path}'
        """Fetches the agent card from the specified URL.

        Args:
            http_kwargs: Optional dictionary of keyword arguments to pass to the
                underlying httpx.get request.

        Returns:
            An `AgentCard` object representing the agent's capabilities.

        Raises:
            A2AClientHTTPError: If an HTTP error occurs during the request.
            A2AClientJSONError: If the response body cannot be decoded as JSON
                or validated against the AgentCard schema.
        """
        try:
            response = await self.httpx_client.get(
                public_card_url,
                **(http_kwargs or {}),
            )
            response.raise_for_status()
            public_agent_card_data = response.json()
            logger.info(
                'Successfully fetched public agent card data: %s',
                public_agent_card_data,
            )  # Added for verbosity
            # print(f"DEBUG: Fetched public agent card data:\n{json.dumps(public_agent_card_data, indent=2)}") # Added for direct output
            agent_card = AgentCard.model_validate(public_agent_card_data)
        except httpx.HTTPStatusError as e:
            raise A2AClientHTTPError(
                e.response.status_code,
                f'Failed to fetch public agent card from {public_card_url}: {e}',
            ) from e
        except json.JSONDecodeError as e:
            raise A2AClientJSONError(
                f'Failed to parse JSON for public agent card from {public_card_url}: {e}'
            ) from e
        except httpx.RequestError as e:
            raise A2AClientHTTPError(
                503,
                f'Network communication error fetching public agent card from {public_card_url}: {e}',
            ) from e

        # Check for supportsAuthenticatedExtendedCard
        if agent_card.supportsAuthenticatedExtendedCard:
            # Construct URL for the extended card.
            # The extended card URL is relative to the agent's base URL specified *in* the agent card.
            if not agent_card.url:
                logger.warning(
                    'Agent card (from %s) indicates support for an extended card '
                    "but does not specify its own base 'url' field. "
                    'Cannot fetch extended card. Proceeding with public card.',
                    public_card_url,
                )
                return agent_card

            extended_card_base_url = agent_card.url.rstrip('/')
            full_extended_card_url = (
                f'{extended_card_base_url}/{self.extended_agent_card_path}'
            )

            logger.info(
                'Attempting to fetch extended agent card from %s',
                full_extended_card_url,
            )
            try:
                # Make another GET request for the extended card
                # Note: Authentication headers will be added here when auth is implemented.
                extended_response = await self.httpx_client.get(
                    full_extended_card_url,
                    **(http_kwargs or {}),  # Passing original http_kwargs
                )
                extended_response.raise_for_status()
                extended_agent_card_data = extended_response.json()
                logger.info(
                    'Successfully fetched extended agent card data: %s',
                    extended_agent_card_data,
                )  # Added for verbosity
                print(
                    f'DEBUG: Fetched extended agent card data:\n{json.dumps(extended_agent_card_data, indent=2)}'
                )  # Added for direct output
                # This new card data replaces the old one entirely
                agent_card = AgentCard.model_validate(extended_agent_card_data)
                logger.info(
                    'Successfully fetched and using extended agent card from %s',
                    full_extended_card_url,
                )
            except (
                httpx.HTTPStatusError,
                httpx.RequestError,
                json.JSONDecodeError,
                ValidationError,
            ) as e:
                logger.warning(
                    'Failed to fetch or parse extended agent card from %s. Error: %s. '
                    'Proceeding with the initially fetched public agent card.',
                    full_extended_card_url,
                    e,
                )
                # Fallback to the already parsed public_agent_card (which is 'agent_card' at this point)

        return agent_card


@trace_class(kind=SpanKind.CLIENT)
class A2AClient:
    """A2A Client for interacting with an A2A agent."""

    def __init__(
        self,
        httpx_client: httpx.AsyncClient,
        agent_card: AgentCard | None = None,
        url: str | None = None,
    ):
        """Initializes the A2AClient.

        Requires either an `AgentCard` or a direct `url` to the agent's RPC endpoint.

        Args:
            httpx_client: An async HTTP client instance (e.g., httpx.AsyncClient).
            agent_card: The agent card object. If provided, `url` is taken from `agent_card.url`.
            url: The direct URL to the agent's A2A RPC endpoint. Required if `agent_card` is None.

        Raises:
            ValueError: If neither `agent_card` nor `url` is provided.
        """
        if agent_card:
            self.url = agent_card.url
        elif url:
            self.url = url
        else:
            raise ValueError('Must provide either agent_card or url')

        self.httpx_client = httpx_client

    @staticmethod
    async def get_client_from_agent_card_url(
        httpx_client: httpx.AsyncClient,
        base_url: str,
        agent_card_path: str = '/.well-known/agent.json',
        http_kwargs: dict[str, Any] | None = None,
    ) -> 'A2AClient':
        """Fetches the AgentCard and initializes an A2A client.

        Args:
            httpx_client: An async HTTP client instance (e.g., httpx.AsyncClient).
            base_url: The base URL of the agent's host.
            agent_card_path: The path to the agent card endpoint, relative to the base URL.
            http_kwargs: Optional dictionary of keyword arguments to pass to the
                underlying httpx.get request when fetching the agent card.

        Returns:
            An initialized `A2AClient` instance.

        Raises:
            A2AClientHTTPError: If an HTTP error occurs fetching the agent card.
            A2AClientJSONError: If the agent card response is invalid.
        """
        agent_card: AgentCard = await A2ACardResolver(
            httpx_client, base_url=base_url, agent_card_path=agent_card_path
        ).get_agent_card(http_kwargs=http_kwargs)
        return A2AClient(httpx_client=httpx_client, agent_card=agent_card)

    async def send_message(
        self,
        request: SendMessageRequest,
        *,
        http_kwargs: dict[str, Any] | None = None,
    ) -> SendMessageResponse:
        """Sends a non-streaming message request to the agent.

        Args:
            request: The `SendMessageRequest` object containing the message and configuration.
            http_kwargs: Optional dictionary of keyword arguments to pass to the
                underlying httpx.post request.

        Returns:
            A `SendMessageResponse` object containing the agent's response (Task or Message) or an error.

        Raises:
            A2AClientHTTPError: If an HTTP error occurs during the request.
            A2AClientJSONError: If the response body cannot be decoded as JSON or validated.
        """
        if not request.id:
            request.id = str(uuid4())

        return SendMessageResponse(
            **await self._send_request(
                request.model_dump(mode='json', exclude_none=True),
                http_kwargs,
            )
        )

    async def send_message_streaming(
        self,
        request: SendStreamingMessageRequest,
        *,
        http_kwargs: dict[str, Any] | None = None,
    ) -> AsyncGenerator[SendStreamingMessageResponse]:
        """Sends a streaming message request to the agent and yields responses as they arrive.

        This method uses Server-Sent Events (SSE) to receive a stream of updates from the agent.

        Args:
            request: The `SendStreamingMessageRequest` object containing the message and configuration.
            http_kwargs: Optional dictionary of keyword arguments to pass to the
                underlying httpx.post request. A default `timeout=None` is set but can be overridden.

        Yields:
            `SendStreamingMessageResponse` objects as they are received in the SSE stream.
            These can be Task, Message, TaskStatusUpdateEvent, or TaskArtifactUpdateEvent.

        Raises:
            A2AClientHTTPError: If an HTTP or SSE protocol error occurs during the request.
            A2AClientJSONError: If an SSE event data cannot be decoded as JSON or validated.
        """
        if not request.id:
            request.id = str(uuid4())

        # Default to no timeout for streaming, can be overridden by http_kwargs
        http_kwargs_with_timeout: dict[str, Any] = {
            'timeout': None,
            **(http_kwargs or {}),
        }

        async with aconnect_sse(
            self.httpx_client,
            'POST',
            self.url,
            json=request.model_dump(mode='json', exclude_none=True),
            **http_kwargs_with_timeout,
        ) as event_source:
            try:
                async for sse in event_source.aiter_sse():
                    yield SendStreamingMessageResponse(**json.loads(sse.data))
            except SSEError as e:
                raise A2AClientHTTPError(
                    400,
                    f'Invalid SSE response or protocol error: {e}',
                ) from e
            except json.JSONDecodeError as e:
                raise A2AClientJSONError(str(e)) from e
            except httpx.RequestError as e:
                raise A2AClientHTTPError(
                    503, f'Network communication error: {e}'
                ) from e

    async def _send_request(
        self,
        rpc_request_payload: dict[str, Any],
        http_kwargs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Sends a non-streaming JSON-RPC request to the agent.

        Args:
            rpc_request_payload: JSON RPC payload for sending the request.
            http_kwargs: Optional dictionary of keyword arguments to pass to the
                underlying httpx.post request.

        Returns:
            The JSON response payload as a dictionary.

        Raises:
            A2AClientHTTPError: If an HTTP error occurs during the request.
            A2AClientJSONError: If the response body cannot be decoded as JSON.
        """
        try:
            response = await self.httpx_client.post(
                self.url, json=rpc_request_payload, **(http_kwargs or {})
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            raise A2AClientHTTPError(e.response.status_code, str(e)) from e
        except json.JSONDecodeError as e:
            raise A2AClientJSONError(str(e)) from e
        except httpx.RequestError as e:
            raise A2AClientHTTPError(
                503, f'Network communication error: {e}'
            ) from e

    async def get_task(
        self,
        request: GetTaskRequest,
        *,
        http_kwargs: dict[str, Any] | None = None,
    ) -> GetTaskResponse:
        """Retrieves the current state and history of a specific task.

        Args:
            request: The `GetTaskRequest` object specifying the task ID and history length.
            http_kwargs: Optional dictionary of keyword arguments to pass to the
                underlying httpx.post request.

        Returns:
            A `GetTaskResponse` object containing the Task or an error.

        Raises:
            A2AClientHTTPError: If an HTTP error occurs during the request.
            A2AClientJSONError: If the response body cannot be decoded as JSON or validated.
        """
        if not request.id:
            request.id = str(uuid4())

        return GetTaskResponse(
            **await self._send_request(
                request.model_dump(mode='json', exclude_none=True),
                http_kwargs,
            )
        )

    async def cancel_task(
        self,
        request: CancelTaskRequest,
        *,
        http_kwargs: dict[str, Any] | None = None,
    ) -> CancelTaskResponse:
        """Requests the agent to cancel a specific task.

        Args:
            request: The `CancelTaskRequest` object specifying the task ID.
            http_kwargs: Optional dictionary of keyword arguments to pass to the
                underlying httpx.post request.

        Returns:
            A `CancelTaskResponse` object containing the updated Task with canceled status or an error.

        Raises:
            A2AClientHTTPError: If an HTTP error occurs during the request.
            A2AClientJSONError: If the response body cannot be decoded as JSON or validated.
        """
        if not request.id:
            request.id = str(uuid4())

        return CancelTaskResponse(
            **await self._send_request(
                request.model_dump(mode='json', exclude_none=True),
                http_kwargs,
            )
        )

    async def set_task_callback(
        self,
        request: SetTaskPushNotificationConfigRequest,
        *,
        http_kwargs: dict[str, Any] | None = None,
    ) -> SetTaskPushNotificationConfigResponse:
        """Sets or updates the push notification configuration for a specific task.

        Args:
            request: The `SetTaskPushNotificationConfigRequest` object specifying the task ID and configuration.
            http_kwargs: Optional dictionary of keyword arguments to pass to the
                underlying httpx.post request.

        Returns:
            A `SetTaskPushNotificationConfigResponse` object containing the confirmation or an error.

        Raises:
            A2AClientHTTPError: If an HTTP error occurs during the request.
            A2AClientJSONError: If the response body cannot be decoded as JSON or validated.
        """
        if not request.id:
            request.id = str(uuid4())

        return SetTaskPushNotificationConfigResponse(
            **await self._send_request(
                request.model_dump(mode='json', exclude_none=True),
                http_kwargs,
            )
        )

    async def get_task_callback(
        self,
        request: GetTaskPushNotificationConfigRequest,
        *,
        http_kwargs: dict[str, Any] | None = None,
    ) -> GetTaskPushNotificationConfigResponse:
        """Retrieves the push notification configuration for a specific task.

        Args:
            request: The `GetTaskPushNotificationConfigRequest` object specifying the task ID.
            http_kwargs: Optional dictionary of keyword arguments to pass to the
                underlying httpx.post request.

        Returns:
            A `GetTaskPushNotificationConfigResponse` object containing the configuration or an error.

        Raises:
            A2AClientHTTPError: If an HTTP error occurs during the request.
            A2AClientJSONError: If the response body cannot be decoded as JSON or validated.
        """
        if not request.id:
            request.id = str(uuid4())

        return GetTaskPushNotificationConfigResponse(
            **await self._send_request(
                request.model_dump(mode='json', exclude_none=True),
                http_kwargs,
            )
        )
