import copy
import threading
from typing import Any
from typing import Callable
from typing import Container
from typing import Dict
from typing import List
from typing import Optional
from typing import Sequence
from typing import Set
from typing import Tuple
from typing import Union

import optuna
from optuna import distributions
from optuna._typing import JSONSerializable
from optuna.storages import BaseStorage
from optuna.storages._heartbeat import BaseHeartbeat
from optuna.storages._rdb.storage import RDBStorage
from optuna.study._frozen import FrozenStudy
from optuna.study._study_direction import StudyDirection
from optuna.trial import FrozenTrial
from optuna.trial import TrialState


class _StudyInfo:
    def __init__(self) -> None:
        # Trial number to corresponding FrozenTrial.
        self.trials: Dict[int, FrozenTrial] = {}
        # A list of trials which do not require storage access to read latest attributes.
        self.finished_trial_ids: Set[int] = set()
        # Cache distributions to avoid storage access on distribution consistency check.
        self.param_distribution: Dict[str, distributions.BaseDistribution] = {}
        self.directions: Optional[List[StudyDirection]] = None
        self.name: Optional[str] = None


class _CachedStorage(BaseStorage, BaseHeartbeat):
    """A wrapper class of storage backends.

    This class is used in :func:`~optuna.get_storage` function and automatically
    wraps :class:`~optuna.storages.RDBStorage` class.

    :class:`~optuna.storages._CachedStorage` meets the following **Consistency** and
    **Data persistence** requirements.

    **Consistency**

    :class:`~optuna.storages._CachedStorage` will return the latest values of any attributes
    of a study and a trial by syncing with the backend when necessary. In this class, a method
    named `read_trials_from_remote_storage(study_id)` is specially defined for this purpose.
    If the method is called, any successive reads on the `state` attribute of a `Trial`
    are guaranteed to return the same or more recent values than the value at the time of the
    call to the method.

    Let `T` be a `Trial`. Let `P` be the process that last updated the `state` attribute of `T`.
    Then, any reads on any attributes of `T` are guaranteed to return the same or
    more recent values than any writes by `P` on the attribute before `P` updated
    the `state` attribute of `T`.
    The same applies for `user_attrs', 'system_attrs' and 'intermediate_values` attributes.

    **Data persistence**

    :class:`~optuna.storages._CachedStorage` does not guarantee that write operations are logged
    into a persistent storage, even when write methods succeed.
    Thus, when process failure occurs, some writes might be lost.
    As exceptions, when a persistent storage is available, any writes on any attributes
    of `Study` and writes on `state` of `Trial` are guaranteed to be persistent.
    Additionally, any preceding writes on any attributes of `Trial` are guaranteed to
    be written into a persistent storage before writes on `state` of `Trial` succeed.
    The same applies for `param`, `user_attrs', 'system_attrs' and 'intermediate_values`
    attributes.

    Args:
        backend:
            :class:`~optuna.storages.RDBStorage` class instance to wrap.
    """

    def __init__(self, backend: RDBStorage) -> None:
        self._backend = backend
        self._studies: Dict[int, _StudyInfo] = {}
        self._trial_id_to_study_id_and_number: Dict[int, Tuple[int, int]] = {}
        self._study_id_and_number_to_trial_id: Dict[Tuple[int, int], int] = {}
        self._lock = threading.Lock()

    def __getstate__(self) -> Dict[Any, Any]:
        state = self.__dict__.copy()
        del state["_lock"]
        return state

    def __setstate__(self, state: Dict[Any, Any]) -> None:
        self.__dict__.update(state)
        self._lock = threading.Lock()

    def create_new_study(
        self, directions: Sequence[StudyDirection], study_name: Optional[str] = None
    ) -> int:
        study_id = self._backend.create_new_study(directions=directions, study_name=study_name)
        with self._lock:
            study = _StudyInfo()
            study.name = study_name
            study.directions = list(directions)
            self._studies[study_id] = study

        return study_id

    def delete_study(self, study_id: int) -> None:
        with self._lock:
            if study_id in self._studies:
                for trial_id in self._studies[study_id].trials:
                    if trial_id in self._trial_id_to_study_id_and_number:
                        del self._study_id_and_number_to_trial_id[
                            self._trial_id_to_study_id_and_number[trial_id]
                        ]
                        del self._trial_id_to_study_id_and_number[trial_id]
                del self._studies[study_id]

        self._backend.delete_study(study_id)

    def set_study_user_attr(self, study_id: int, key: str, value: Any) -> None:
        self._backend.set_study_user_attr(study_id, key, value)

    def set_study_system_attr(self, study_id: int, key: str, value: JSONSerializable) -> None:
        self._backend.set_study_system_attr(study_id, key, value)

    def get_study_id_from_name(self, study_name: str) -> int:
        return self._backend.get_study_id_from_name(study_name)

    def get_study_name_from_id(self, study_id: int) -> str:
        with self._lock:
            if study_id in self._studies:
                name = self._studies[study_id].name
                if name is not None:
                    return name

        name = self._backend.get_study_name_from_id(study_id)
        with self._lock:
            if study_id not in self._studies:
                self._studies[study_id] = _StudyInfo()
            self._studies[study_id].name = name
        return name

    def get_study_directions(self, study_id: int) -> List[StudyDirection]:
        with self._lock:
            if study_id in self._studies:
                directions = self._studies[study_id].directions
                if directions is not None:
                    return directions

        directions = self._backend.get_study_directions(study_id)
        with self._lock:
            if study_id not in self._studies:
                self._studies[study_id] = _StudyInfo()
            self._studies[study_id].directions = directions
        return directions

    def get_study_user_attrs(self, study_id: int) -> Dict[str, Any]:
        return self._backend.get_study_user_attrs(study_id)

    def get_study_system_attrs(self, study_id: int) -> Dict[str, Any]:
        return self._backend.get_study_system_attrs(study_id)

    def get_all_studies(self) -> List[FrozenStudy]:
        return self._backend.get_all_studies()

    def create_new_trial(self, study_id: int, template_trial: Optional[FrozenTrial] = None) -> int:
        frozen_trial = self._backend._create_new_trial(study_id, template_trial)
        trial_id = frozen_trial._trial_id
        with self._lock:
            if study_id not in self._studies:
                self._studies[study_id] = _StudyInfo()
            study = self._studies[study_id]
            self._add_trials_to_cache(study_id, [frozen_trial])
            # Since finished trials will not be modified by any worker, we do not
            # need storage access for them.
            if frozen_trial.state.is_finished():
                study.finished_trial_ids.add(frozen_trial._trial_id)
        return trial_id

    def set_trial_param(
        self,
        trial_id: int,
        param_name: str,
        param_value_internal: float,
        distribution: distributions.BaseDistribution,
    ) -> None:
        self._backend.set_trial_param(trial_id, param_name, param_value_internal, distribution)

    def get_trial_id_from_study_id_trial_number(self, study_id: int, trial_number: int) -> int:
        key = (study_id, trial_number)
        with self._lock:
            if key in self._study_id_and_number_to_trial_id:
                return self._study_id_and_number_to_trial_id[key]

        return self._backend.get_trial_id_from_study_id_trial_number(study_id, trial_number)

    def get_best_trial(self, study_id: int) -> FrozenTrial:
        return self._backend.get_best_trial(study_id)

    def set_trial_state_values(
        self, trial_id: int, state: TrialState, values: Optional[Sequence[float]] = None
    ) -> bool:
        return self._backend.set_trial_state_values(trial_id, state=state, values=values)

    def set_trial_intermediate_value(
        self, trial_id: int, step: int, intermediate_value: float
    ) -> None:
        self._backend.set_trial_intermediate_value(trial_id, step, intermediate_value)

    def set_trial_user_attr(self, trial_id: int, key: str, value: Any) -> None:
        self._backend.set_trial_user_attr(trial_id, key=key, value=value)

    def set_trial_system_attr(self, trial_id: int, key: str, value: JSONSerializable) -> None:
        self._backend.set_trial_system_attr(trial_id, key=key, value=value)

    def _get_cached_trial(self, trial_id: int) -> Optional[FrozenTrial]:
        if trial_id not in self._trial_id_to_study_id_and_number:
            return None
        study_id, number = self._trial_id_to_study_id_and_number[trial_id]
        study = self._studies[study_id]
        return study.trials[number] if trial_id in study.finished_trial_ids else None

    def get_trial(self, trial_id: int) -> FrozenTrial:
        with self._lock:
            trial = self._get_cached_trial(trial_id)
            if trial is not None:
                return trial

        return self._backend.get_trial(trial_id)

    def get_all_trials(
        self,
        study_id: int,
        deepcopy: bool = True,
        states: Optional[Container[TrialState]] = None,
    ) -> List[FrozenTrial]:
        if study_id not in self._studies:
            self.read_trials_from_remote_storage(study_id)

        with self._lock:
            study = self._studies[study_id]
            # We need to sort trials by their number because some samplers assume this behavior.
            # The following two lines are latency-sensitive.

            trials: Union[Dict[int, FrozenTrial], List[FrozenTrial]]

            if states is not None:
                trials = {number: t for number, t in study.trials.items() if t.state in states}
            else:
                trials = study.trials
            trials = list(sorted(trials.values(), key=lambda t: t.number))
            return copy.deepcopy(trials) if deepcopy else trials

    def read_trials_from_remote_storage(self, study_id: int) -> None:
        with self._lock:
            if study_id not in self._studies:
                self._studies[study_id] = _StudyInfo()
            study = self._studies[study_id]
            trials = self._backend._get_trials(
                study_id, states=None, excluded_trial_ids=study.finished_trial_ids
            )
            if trials:
                self._add_trials_to_cache(study_id, trials)
                for trial in trials:
                    if trial.state.is_finished():
                        study.finished_trial_ids.add(trial._trial_id)

    def _add_trials_to_cache(self, study_id: int, trials: List[FrozenTrial]) -> None:
        study = self._studies[study_id]
        for trial in trials:
            self._trial_id_to_study_id_and_number[trial._trial_id] = (
                study_id,
                trial.number,
            )
            self._study_id_and_number_to_trial_id[(study_id, trial.number)] = trial._trial_id
            study.trials[trial.number] = trial

    def record_heartbeat(self, trial_id: int) -> None:
        self._backend.record_heartbeat(trial_id)

    def _get_stale_trial_ids(self, study_id: int) -> List[int]:
        return self._backend._get_stale_trial_ids(study_id)

    def get_heartbeat_interval(self) -> Optional[int]:
        return self._backend.get_heartbeat_interval()

    def get_failed_trial_callback(self) -> Optional[Callable[["optuna.Study", FrozenTrial], None]]:
        return self._backend.get_failed_trial_callback()
