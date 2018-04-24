from collections import defaultdict
from datetime import datetime
import json

from sqlalchemy.engine import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy import orm
from typing import Any  # NOQA
from typing import Dict  # NOQA
from typing import List  # NOQA
from typing import Optional  # NOQA
import uuid

from pfnopt import distributions
from pfnopt import logging
from pfnopt.storages.base import BaseStorage
from pfnopt.storages.base import SYSTEM_ATTRS_KEY
from pfnopt.storages import models
import pfnopt.trial as trial_module
from pfnopt.trial import State
from pfnopt import version


class RDBStorage(BaseStorage):

    def __init__(self, url, connect_args=None):
        # type: (str, Optional[Dict[str, Any]]) -> None

        connect_args = connect_args or {}
        self.engine = create_engine(url, connect_args=connect_args)
        self.scoped_session = orm.scoped_session(orm.sessionmaker(bind=self.engine))
        models.BaseModel.metadata.create_all(self.engine)
        self._check_table_schema_compatibility()
        self.logger = logging.get_logger(__name__)

    def create_new_study_id(self):
        # type: () -> int

        session = self.scoped_session()

        while True:
            study_uuid = str(uuid.uuid4())
            study = models.StudyModel.find_by_uuid(study_uuid, session)
            if study is None:
                break

        study = models.StudyModel(study_uuid=study_uuid)
        session.add(study)
        session.commit()

        # Set system attribute key and empty value.
        self.set_study_user_attr(study.study_id, SYSTEM_ATTRS_KEY, {})

        self.logger.info('A new study created with UUID: {}'.format(study.study_uuid))

        return study.study_id

    def set_study_user_attr(self, study_id, key, value):
        # type: (int, str, Any) -> None

        session = self.scoped_session()

        # Check if the study already exists.
        models.StudyModel.find_by_id(study_id, session, allow_none=False)

        attribute = models.StudyUserAttributeModel.find_by_study_id_and_key(study_id, key, session)
        if attribute is None:
            attribute = models.StudyUserAttributeModel(
                study_id=study_id, key=key, value_json=json.dumps(value))
            session.add(attribute)
        else:
            attribute.value_json = json.dumps(value)

        session.commit()

    def get_study_id_from_uuid(self, study_uuid):
        # type: (str) -> int

        session = self.scoped_session()
        study = models.StudyModel.find_by_uuid(study_uuid, session, allow_none=False)

        return study.study_id

    def get_study_uuid_from_id(self, study_id):
        # type: (int) -> str

        session = self.scoped_session()
        study = models.StudyModel.find_by_id(study_id, session, allow_none=False)

        return study.study_uuid

    def get_study_user_attrs(self, study_id):
        # type: (int) -> Dict[str, Any]

        session = self.scoped_session()
        attributes = models.StudyUserAttributeModel.where_study_id(study_id, session)

        return {attr.key: json.loads(attr.value_json) for attr in attributes}

    def set_trial_param_distribution(self, trial_id, param_name, distribution):
        # type: (int, str, distributions.BaseDistribution) -> None

        session = self.scoped_session()

        param_distribution = models.TrialParamDistributionModel(
            trial_id=trial_id,
            param_name=param_name,
            distribution_json=distributions.distribution_to_json(distribution)
        )

        param_distribution.check_and_add(session)
        session.commit()

    def create_new_trial_id(self, study_id):
        # type: (int) -> int

        session = self.scoped_session()

        trial = models.TrialModel(
            study_id=study_id,
            state=State.RUNNING,
            user_attributes_json=json.dumps({SYSTEM_ATTRS_KEY: {}})
        )

        session.add(trial)
        session.commit()

        return trial.trial_id

    def set_trial_state(self, trial_id, state):
        # type: (int, trial_module.State) -> None

        session = self.scoped_session()

        trial = models.TrialModel.find_by_id(trial_id, session, allow_none=False)
        trial.state = state
        if state.is_finished():
            trial.datetime_complete = datetime.now()

        session.commit()

    def set_trial_param(self, trial_id, param_name, param_value):
        # type: (int, str, float) -> None

        session = self.scoped_session()

        trial = models.TrialModel.find_by_id(trial_id, session, allow_none=False)
        param_distribution = models.TrialParamDistributionModel.find_by_trial_and_param_name(
            trial, param_name, session, allow_none=False)

        # check if the parameter already exists
        param = models.TrialParamModel.find_by_trial_and_param_name(trial, param_name, session)
        if param is not None:
            assert param.param_value == param_value
            return

        param = models.TrialParamModel(
            trial_id=trial_id,
            param_distribution_id=param_distribution.param_distribution_id,
            param_value=param_value
        )

        session.add(param)
        try:
            session.commit()
        except IntegrityError as e:
            self.logger.debug(
                'Caught {}. This happens due to a known race condition. Another process/thread '
                'might have committed a record with the same unique key.'.format(repr(e)))
            session.rollback()

    def set_trial_value(self, trial_id, value):
        # type: (int, float) -> None

        session = self.scoped_session()

        trial = models.TrialModel.find_by_id(trial_id, session, allow_none=False)
        trial.value = value

        session.commit()

    def set_trial_intermediate_value(self, trial_id, step, intermediate_value):
        # type: (int, int, float) -> None

        session = self.scoped_session()

        # the following line is to check that the specified trial_id exists in DB.
        trial = models.TrialModel.find_by_id(trial_id, session, allow_none=False)

        # check if the value at the same step already exists
        trial_value = models.TrialValueModel.find_by_trial_and_step(trial, step, session)
        if trial_value is not None:
            assert trial_value.value == intermediate_value
            return

        trial_value = models.TrialValueModel(
            trial_id=trial_id,
            step=step,
            value=intermediate_value
        )

        session.add(trial_value)
        try:
            session.commit()
        except IntegrityError as e:
            self.logger.debug(
                'Caught {}. This happens due to a known race condition. Another process/thread '
                'might have committed a record with the same unique key.'.format(repr(e)))
            session.rollback()

    def set_trial_user_attr(self, trial_id, key, value):
        # type: (int, str, Any) -> None

        session = self.scoped_session()

        trial = models.TrialModel.find_by_id(trial_id, session, allow_none=False)

        loaded_json = json.loads(trial.user_attributes_json)
        loaded_json[key] = value
        trial.user_attributes_json = json.dumps(loaded_json)

        session.commit()

    def get_trial(self, trial_id):
        # type: (int) -> trial_module.Trial

        session = self.scoped_session()
        trial = session.query(models.TrialModel).filter(models.TrialModel.trial_id == trial_id).one()
        params = session.query(models.TrialParamModel).filter(models.TrialParamModel.trial_id == trial_id).all()
        values = session.query(models.TrialValueModel).filter(models.TrialValueModel.trial_id == trial_id).all()

        return self._merge_trials_orm([trial], params, values)[0]

    def get_all_trials(self, study_id):
        # type: (int) -> List[trial_module.Trial]

        session = self.scoped_session()
        trials = session.query(models.TrialModel).filter(models.TrialModel.study_id == study_id).all()
        params = session.query(models.TrialParamModel).join(models.TrialModel). \
            filter(models.TrialModel.study_id == study_id).all()
        values = session.query(models.TrialValueModel).join(models.TrialModel). \
            filter(models.TrialModel.study_id == study_id).all()

        return self._merge_trials_orm(trials, params, values)

    @staticmethod
    def _merge_trials_orm(
            trials,  # type: List[models.TrialModel]
            trial_params,   # type: List[models.TrialParamModel]
            trial_intermediate_values  # type: List[models.TrialValueModel]
    ):
        # type: (...) -> List[trial_module.Trial]

        id_to_trial = {}
        for trial in trials:
            id_to_trial[trial.trial_id] = trial

        id_to_trial_params = defaultdict(list)  # type: Dict[int, List[models.TrialParamModel]]
        for param in trial_params:
            id_to_trial_params[param.trial_id].append(param)

        id_to_intermediate_values = defaultdict(list)  # type: Dict[int, List[models.TrialValueModel]]
        for value in trial_intermediate_values:
            id_to_intermediate_values[value.trial_id].append(value)

        result = []
        for trial_id, trial in id_to_trial.items():
            params = {}
            params_in_internal_repr = {}
            for param in id_to_trial_params[trial_id]:
                distribution = \
                    distributions.json_to_distribution(param.param_distribution.distribution_json)
                params[param.param_distribution.param_name] = \
                    distribution.to_external_repr(param.param_value)
                params_in_internal_repr[param.param_distribution.param_name] = param.param_value

            intermediate_values = {}
            for value in id_to_intermediate_values[trial_id]:
                intermediate_values[value.step] = value.value

            result.append(trial_module.Trial(
                trial_id=trial_id,
                state=trial.state,
                params=params,
                user_attrs=json.loads(trial.user_attributes_json),
                value=trial.value,
                intermediate_values=intermediate_values,
                params_in_internal_repr=params_in_internal_repr,
                datetime_start=trial.datetime_start,
                datetime_complete=trial.datetime_complete
            ))

        return result

    def _check_table_schema_compatibility(self):
        # type: () -> None

        session = self.scoped_session()

        version_info = session.query(models.VersionInfoModel).one_or_none()
        if version_info is None:
            version_info = models.VersionInfoModel()
            version_info.schema_version = models.SCHEMA_VERSION
            version_info.library_version = version.__version__
            session.add(version_info)
            try:
                session.commit()
            except IntegrityError as e:
                self.logger.debug(
                    'Ignoring {}. This happens due to a timing issue during initial setup of {} '
                    'table among multi threads/processes/nodes.'.format(
                        repr(e), models.VersionInfoModel.__tablename__))
                session.rollback()
        else:
            if version_info.schema_version != models.SCHEMA_VERSION:
                raise RuntimeError(
                    'The runtime pfnopt version {} is no longer compatible with the table schema '
                    '(set up by pfnopt {}).'.format(
                        version.__version__, version_info.library_version))

    def remove_session(self):
        # type: () -> None

        """Removes the current session.

        A session is stored in SQLAlchemy's ThreadLocalRegistry for each thread. This method
        closes and removes the session which is associated to the current thread. Particularly,
        under multi-thread use cases, it is important to call this method *from each thread*.
        Otherwise, all sessions and their associated DB connections are destructed by a thread
        that occasionally invoked the garbage collector. By default, it is not allowed to touch
        a SQLite connection from threads other than the thread that created the connection.
        Therefore, we need to explicitly close the connection from each thread.

        """

        self.scoped_session.remove()

    def __del__(self):
        # type: () -> None

        # This destructor calls remove_session to explicitly close the DB connection. We need this
        # because DB connections created in SQLAlchemy are not automatically closed by reference
        # counters, so it is not guaranteed that they are released by correct threads (for more
        # information, please see the docstring of remove_session).

        self.remove_session()
