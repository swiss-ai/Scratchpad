import abc
import torch
import typing
from typing import TYPE_CHECKING, Set, Type

if TYPE_CHECKING:
    from scratchpad.scheduler.schedule_batch import ScheduleBatch


class BatchedPenalizerOrchestrator:
    def __init__(
        self,
        vocab_size: int,
        batch: "ScheduleBatch",
        penalizers: Set[Type["_BatchedPenalizer"]],
    ):
        self.vocab_size = vocab_size
        self.batch = batch
        self.device = batch.device
        self.penalizers = {Penalizer: Penalizer(self) for Penalizer in penalizers}

        is_required = False
        for penalizer in self.penalizers.values():
            pen_is_required = penalizer.prepare_if_required()
            is_required |= pen_is_required
        self.is_required = is_required

    def reqs(self):
        return self.batch.reqs

    def cumulate_output_tokens(self, output_ids: torch.Tensor):
        """
        Feed the output tokens to the penalizers.

        Args:
            output_ids (torch.Tensor): The output tokens.
        """
        for penalizer in self.penalizers.values():
            penalizer.cumulate_output_tokens(output_ids=output_ids)

    def apply(self, logits: torch.Tensor) -> torch.Tensor:
        """
        Apply the penalizers to the logits.
        Note that it may apply the penalizers in-place.

        Args:
            logits (torch.Tensor): The logits to apply the penalizers to.

        Returns:
            torch.Tensor: The logits after applying the penalizers.
        """
        for penalizer in self.penalizers.values():
            penalizer.apply(logits)

    def filter(self, keep_indices: torch.Tensor):
        """
        Filter the penalizers based on the indices to keep in the batch.

        Args:
            keep_indices (torch.Tensor): Tensor of indices to keep in the batch.
        """
        if not self.is_required:
            return

        if len(keep_indices) == 0:
            self.is_required = False
            for penalizer in self.penalizers.values():
                penalizer.teardown()
            return

        is_required = False
        for penalizer in self.penalizers.values():
            tmp_is_required = penalizer.is_required()
            is_required |= tmp_is_required
            if tmp_is_required:
                penalizer.filter(keep_indices=keep_indices)
            else:
                penalizer.teardown()
        self.is_required = is_required

    def merge(self, their: "BatchedPenalizerOrchestrator"):
        """
        Merge the penalizers of another orchestrator into this one.

        Note that this function **must** be called _before_ self.batch.reqs is updated (filtered).
        Each unprepared penalizers would have to be prepared (creating tensors, etc.) first before merging.
        This step requires the original batch.reqs, before it gets merged with other batch.reqs.

        Args:
            their (BatchedPenalizerOrchestrator): The orchestrator to merge into this one.
        """
        if not self.is_required and not their.is_required:
            return

        self.is_required = True
        for penalizer, their_penalizer in their.penalizers.items():
            self.penalizers[penalizer].merge(their_penalizer)


class _TokenIDs:
    """
    A class that wraps token IDs to provide additional utility functions to penalizers.

    Attributes:
        orchestrator (BatchedPenalizerOrchestrator): The orchestrator that this token IDs belong to.
        token_ids (typing.Union[torch.Tensor, typing.List[torch.Tensor]]): The token IDs.
        cached_counts (torch.Tensor): The cached occurrence count tensor.
    """

    orchestrator: BatchedPenalizerOrchestrator
    token_ids: typing.Union[torch.Tensor, typing.List[torch.Tensor]]
    cached_counts: torch.Tensor = None

    def __init__(
        self,
        orchestrator: BatchedPenalizerOrchestrator,
        token_ids: typing.Union[
            typing.List[torch.Tensor], typing.List[typing.List[int]]
        ],
    ):
        self.orchestrator = orchestrator

        if not isinstance(token_ids[0], torch.Tensor):
            token_ids = [
                torch.tensor(
                    data=ids, dtype=torch.int64, device=self.orchestrator.device
                )
                for ids in token_ids
            ]

        self.token_ids = token_ids

    def occurrence_count(self) -> torch.Tensor:
        """
        Returns a tensor of shape (batch_size, vocab_size) where each element is the number of times the corresponding token appears in the batch.

        Returns:
            torch.Tensor: The occurrence count tensor.
        """
        if self.cached_counts is not None:
            return self.cached_counts

        token_ids = self.token_ids

        if isinstance(token_ids, torch.Tensor):
            token_ids = token_ids.unsqueeze(1)

            # needs to be long to be used as index in scatter_add
            if token_ids.dtype != torch.int64:
                token_ids = token_ids.to(torch.int64)

        padded_token_ids = torch.nn.utils.rnn.pad_sequence(
            sequences=token_ids,
            batch_first=True,
            padding_value=self.orchestrator.vocab_size,
        )

        self.cached_counts = torch.zeros(
            size=(self.orchestrator.batch_size(), self.orchestrator.vocab_size + 1),
            dtype=torch.int64,
            device=self.orchestrator.device,
        ).scatter_add_(
            dim=1,
            index=padded_token_ids,
            src=torch.ones_like(padded_token_ids),
        )[
            :, : self.orchestrator.vocab_size
        ]

        return self.cached_counts


class _BatchedPenalizer(abc.ABC):
    """
    An abstract class for a batched penalizer.
    """

    def is_prepared(self) -> bool:
        return self._is_prepared

    def is_required(self) -> bool:
        return self._is_required()

    def prepare(self):
        if not self._is_prepared:
            self._prepare()
            self._is_prepared = True

    def prepare_if_required(self):
        if self._is_required():
            self.prepare()
            return True
        else:
            return False

    def teardown(self):
        self._is_prepared = False

    def cumulate_output_tokens(self, output_ids: torch.Tensor):
        if not self._is_prepared:
            return

        self._cumulate_output_tokens(output_ids=output_ids)

    def apply(self, logits: torch.Tensor) -> torch.Tensor:
        if not self._is_prepared:
            return

        self._apply(logits=logits)

    def filter(self, keep_indices: torch.Tensor):
        if not self._is_prepared:
            return

        self._filter(keep_indices=keep_indices)

    def merge(self, their: "_BatchedPenalizer"):
        if not self._is_prepared and not their._is_prepared:
            return

        self.prepare()
        their.prepare()
        self._merge(their)

    @abc.abstractmethod
    def _is_required(self) -> bool:
        """
        Check if the penalizer is required to be prepared.
        """
        pass

    @abc.abstractmethod
    def _prepare(self):
        """
        Prepare the penalizer.
        Usually, this is where the penalizer initializes its tensors.
        """
        pass

    @abc.abstractmethod
    def _cumulate_output_tokens(self, output_ids: torch.Tensor):
        """
        Cumulate the output tokens.
        Orchestrator will call this function to feed the output tokens to the penalizer.
        """
        pass

    @abc.abstractmethod
    def _apply(self, logits: torch.Tensor) -> torch.Tensor:
        """
        Apply the penalizer to the logits.
        Penalizers can modify the logits in-place if needed.
        """
        pass

    @abc.abstractmethod
    def _filter(self, keep_indices: torch.Tensor):
        """
        Filter the penalizer (tensors or underlying data) based on the indices to keep in the batch.
        """
        pass

    @abc.abstractmethod
    def _merge(self, their: "_BatchedPenalizer"):
        """
        Merge the penalizer with another penalizer.
        """
        pass
