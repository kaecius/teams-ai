"""
Copyright (c) Microsoft Corporation. All rights reserved.
Licensed under the MIT License.
"""

from __future__ import annotations

import asyncio
from typing import Callable, List, Literal, Optional

from botbuilder.core import TurnContext
from botbuilder.schema import Activity, Attachment, Entity

from teams.ai.citations.citations import Appearance, SensitivityUsageInfo
from teams.utils import snippet
from teams.utils.citations import format_citations_response, get_used_citations

from ..ai.citations import AIEntity, ClientCitation
from ..ai.prompts.message import Citation
from ..app_error import ApplicationError
from .streaming_channel_data import StreamingChannelData
from .streaming_entity import StreamingEntity


class StreamingResponse:
    """
    A helper class for streaming responses to the client.
    This class is used to send a series of updates to the client in a single response. The expected
    sequence of calls is:
    `queue_informative_update()`, `queue_text_chunk()`, `queue_text_chunk()`, ..., `end_stream()`.

    Once `end_stream()` is called, the stream ends and no further updates can be sent.
    """

    _context: TurnContext
    _next_sequence: int = 1
    _stream_id: str = ""
    _message: str = ""
    _attachments: List[Attachment] = []
    _ended: bool = False

    _citations: Optional[List[ClientCitation]] = []
    _sensitivity_label: Optional[SensitivityUsageInfo] = None
    _enable_feedback_loop: Optional[bool] = False
    _feedback_loop_type: Optional[Literal["default", "custom"]] = None
    _enable_generated_by_ai_label: Optional[bool] = False

    _queue: List[Callable[[], Activity]] = []
    _queue_sync: Optional[asyncio.Task] = None
    _chunk_queued: bool = False

    def __init__(self, context: TurnContext) -> None:
        """
        Initializes a new instance of the `StreamingResponse` class.
        :param context: The turn context.
        """
        self._context = context

    @property
    def stream_id(self) -> str:
        """
        Access the Streaming Response's stream_id.
        """
        return self._stream_id

    @property
    def message(self) -> str:
        """
        Returns the most recently streamed message.
        """
        return self._message

    @property
    def citations(self) -> Optional[List[ClientCitation]]:
        """
        Returns the list of citations.
        """
        return self._citations

    def set_attachments(self, attachments: List[Attachment]) -> None:
        """
        Sets the attachments to attach to the final chunk.
        :param attachments: List of attachments.
        """
        self._attachments = attachments

    def set_feedback_loop(self, enable_feedback_loop: bool) -> None:
        """
        Sets the feedback loop to enable or disable.
        :param enable_feedback_loop: Boolean value to enable
            or disable feedback loop.
        """
        self._enable_feedback_loop = enable_feedback_loop

    def set_feedback_loop_type(self, feedback_loop_type: Literal["default", "custom"]) -> None:
        """
        Sets the feedback loop to enable or disable.
        :param feedback_loop_type: The type of feedback loop ux to use
        """
        self._feedback_loop_type = feedback_loop_type

    def set_sensitivity_label(self, sensitivity_label: SensitivityUsageInfo) -> None:
        """
        Sets the sensitivity label to attach to the final chunk.
        :param sensitivity_label: SensitivityUsageInfo object.
        """
        self._sensitivity_label = sensitivity_label

    def set_generated_by_ai_label(self, enable_generated_by_ai_label: bool) -> None:
        """
        Sets the generated by AI label to enable or disable.
        :param enable_generated_by_ai_label: Boolean value
            to enable or disable generated by AI label.
        """
        self._enable_generated_by_ai_label = enable_generated_by_ai_label

    def updates_sent(self) -> int:
        """
        Returns the number of updates sent.
        """
        return self._next_sequence - 1

    def set_citations(self, citations: List[Citation]) -> None:
        if len(citations) > 0:
            if not self._citations:
                self._citations = []
            curr_pos = len(self._citations)

            for citation in citations:
                self._citations.append(
                    ClientCitation(
                        position=curr_pos + 1,
                        appearance=Appearance(
                            name=citation.title or f"Document {curr_pos + 1}",
                            abstract=snippet(citation.content, 477),
                        ),
                    )
                )
                curr_pos += 1

    def queue_informative_update(self, text: str) -> None:
        """
        Queue an informative update to be sent to the client.
        :param text: The text of the update to be sent.
        """
        if self._ended:
            raise ApplicationError("The stream has already ended.")

        # Queue a typing activity
        activity = Activity(
            type="typing",
            text=text,
            channel_data=StreamingChannelData(
                stream_type="informative", stream_sequence=self._next_sequence
            ).to_dict(),
        )
        self.queue_activity(lambda: activity)
        self._next_sequence += 1

    def queue_text_chunk(self, text: str, citations: Optional[List[Citation]] = None) -> None:
        # pylint: disable=unused-argument
        """
        Queues a chunk of partial message text to be sent to the client.
        The text we be sent as quickly as possible to the client. Chunks may be combined before
        delivery to the client.
        :param text: The text of the chunk to be sent.
        """
        if self._ended:
            raise ApplicationError("The stream has already ended.")

        self._message += text

        # If there are citations, modify the content so that the sources are numbers
        # instead of [doc1], [doc2], etc.
        self._message = format_citations_response(self._message)

        # Queue the next chunk
        self.queue_next_chunk()

    async def end_stream(self) -> None:
        """
        Ends the stream.
        """
        if self._ended:
            raise ApplicationError("The stream has already ended.")

        # Queue final message
        self._ended = True
        self.queue_next_chunk()

        # Wait for the queue to drain
        await self.wait_for_queue()

    async def wait_for_queue(self):
        """
        Waits for the outoging acitivty queue to be empty.
        """
        if self._queue_sync:
            await self._queue_sync
        else:
            await asyncio.sleep(0)

    def queue_next_chunk(self) -> None:
        """
        Queues the next chunk of text to be sent.
        """

        if self._chunk_queued:
            return

        # Queue a chunk of text to be sent
        self._chunk_queued = True

        def _format_next_chunk() -> Activity:
            """
            Sends the next chunk of text to the client.
            """
            self._chunk_queued = False
            if self._ended:
                return Activity(
                    type="message",
                    text=self._message,
                    attachments=self._attachments,
                    channel_data=StreamingChannelData(stream_type="final").to_dict(),
                )
            activity = Activity(
                type="typing",
                text=self._message,
                channel_data=StreamingChannelData(
                    stream_type="streaming", stream_sequence=self._next_sequence
                ).to_dict(),
            )
            self._next_sequence += 1
            return activity

        self.queue_activity(_format_next_chunk)

    def queue_activity(self, factory: Callable[[], Activity]) -> None:
        """
        Queues an activity to be sent to the client.
        :param activity_factory: A factory function that creates the activity to be sent.
        """
        self._queue.append(factory)

        # If there's no sync in progress, start one
        if not self._queue_sync:
            try:
                self._queue_sync = self.drain_queue()
            except Exception as e:
                raise ApplicationError(
                    "Error occured when sending activity while streaming:"
                ) from e

    def drain_queue(self) -> asyncio.Task:
        async def _drain_queue():
            """
            Sends any queued activities to the client until the queue is empty.
            """
            try:
                while len(self._queue) > 0:
                    # Get next activity from queue
                    factory = self._queue.pop(0)
                    activity = factory()

                    # Send activity
                    await self.send_activity(activity)
            finally:
                # Queue is empty, mark as idle
                self._queue_sync = None

        return asyncio.create_task(_drain_queue())

    async def send_activity(self, activity: Activity) -> None:
        """
        Sends an activity to the client and saves the stream ID returned.
        :param activity: The activity to send.
        """
        # Set activity ID to the assigned stream ID
        channel_data = StreamingChannelData.from_dict(activity.channel_data)

        if self._stream_id:
            channel_data.stream_id = self._stream_id
            activity.channel_data = StreamingChannelData.to_dict(channel_data)

        entity = StreamingEntity(
            stream_id=channel_data.stream_id,
            stream_sequence=channel_data.stream_sequence,
            stream_type=channel_data.stream_type,
        )
        entities: List[Entity] = [entity]
        activity.entities = entities

        # If there are citations, filter out the citations unused in content.
        if self._citations and len(self._citations) > 0 and self._ended is False:
            curr_citations = get_used_citations(self._message, self._citations)
            activity.entities.append(
                AIEntity(
                    additional_type=[],
                    citation=curr_citations if curr_citations else [],
                )
            )

        # Add in Powered by AI feature flags
        if self._ended:
            channel_data = StreamingChannelData.from_dict(activity.channel_data)

            if self._enable_feedback_loop:
                channel_data.feedback_loop_enabled = self._enable_feedback_loop

            if not self._enable_feedback_loop and self._feedback_loop_type:
                channel_data.feedback_loop_type = self._feedback_loop_type

            activity.channel_data = StreamingChannelData.to_dict(channel_data)

            if self._enable_generated_by_ai_label:
                activity.entities.append(
                    AIEntity(
                        additional_type=["AIGeneratedContent"],
                        citation=self._citations if self._citations else [],
                        usage_info=self._sensitivity_label,
                    )
                )

        # Send activity
        response = await self._context.send_activity(activity)
        await asyncio.sleep(1.5)

        # Save assigned stream ID
        if not self._stream_id and response:
            self._stream_id = response.id
