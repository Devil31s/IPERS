from .q_learner import QLearner
from .coma_learner import COMALearner
from .qtran_learner import QLearner as QTranLearner
from .maser_q_learner import maserQLearner
from .q_learner_divide import QDivedeLearner
from .iper import FormDivedeLearner

REGISTRY = {}

REGISTRY["q_learner"] = QLearner
REGISTRY["coma_learner"] = COMALearner
REGISTRY["qtran_learner"] = QTranLearner
REGISTRY["maser_q_learner"] = maserQLearner
REGISTRY["q_learner_divide"] = QDivedeLearner

REGISTRY["iper"] = FormDivedeLearner