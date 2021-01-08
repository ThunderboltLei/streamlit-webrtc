import abc
import threading
import queue
import traceback
import logging
import sys
from typing import Optional, Union

from aiortc import MediaStreamTrack

from av import VideoFrame
import numpy as np


logger = logging.getLogger(__name__)


class VideoTransformerBase(abc.ABC):
    @abc.abstractmethod
    def transform(self, frame: VideoFrame) -> VideoFrame:
        """ Returns a new VideoFrame """


class NoOpVideoTransformer(VideoTransformerBase):
    def transform(self, frame: VideoFrame) -> VideoFrame:
        return frame


class VideoTransformTrack(MediaStreamTrack):
    kind = "video"

    def __init__(
        self, track: MediaStreamTrack, video_transformer: VideoTransformerBase
    ):
        super().__init__()  # don't forget this!
        self.track = track
        self.transformer = video_transformer

    async def recv(self):
        frame = await self.track.recv()
        return self.transformer.transform(frame)


__SENTINEL__ = "__SENTINEL__"


class AsyncVideoTransformTrack(MediaStreamTrack):
    kind = "video"

    def __init__(
        self,
        track: MediaStreamTrack,
        video_transformer: VideoTransformerBase,
        stop_timeout: Optional[float] = None,
    ):
        super().__init__()  # don't forget this!
        self.track = track
        self.transformer = video_transformer

        self._thread = threading.Thread(target=self._run_worker_thread)
        self._in_queue = queue.Queue()
        self._latest_result_img_lock = threading.Lock()

        self._busy = False
        self._latest_result_img: Union[np.ndarray, None] = None

        self._thread.start()

        self.stop_timeout = stop_timeout

    def _run_worker_thread(self):
        try:
            self._worker_thread()
        except Exception:
            logger.error("Error occurred in the WebRTC thread:")

            exc_type, exc_value, exc_traceback = sys.exc_info()
            for tb in traceback.format_exception(exc_type, exc_value, exc_traceback):
                for tbline in tb.rstrip().splitlines():
                    logger.error(tbline.rstrip())

    def _worker_thread(self):
        while True:
            item = self._in_queue.get()
            if item == __SENTINEL__:
                break

            stop_requested = False
            while not self._in_queue.empty():
                item = self._in_queue.get_nowait()
                if item == __SENTINEL__:
                    stop_requested = True
            if stop_requested:
                break

            if item is None:
                raise Exception("A queued item is unexpectedly None")

            output = self.transformer.transform(item)

            with self._latest_result_img_lock:
                self._latest_result_img = output.to_ndarray(
                    format="bgr24"
                )  # TODO: Rethink VideoTransformer interface to return image array

    def stop(self):
        self._in_queue.put(__SENTINEL__)
        self._thread.join(self.stop_timeout)

        return super().stop()

    async def recv(self):
        frame = await self.track.recv()
        self._in_queue.put(frame)

        with self._latest_result_img_lock:
            if self._latest_result_img is not None:
                # rebuild a VideoFrame, preserving timing information
                new_frame = VideoFrame.from_ndarray(
                    self._latest_result_img, format="bgr24"
                )
                new_frame.pts = frame.pts
                new_frame.time_base = frame.time_base
                return new_frame
            else:
                return frame
