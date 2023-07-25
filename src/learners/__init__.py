from .q_learner import QLearner
from .coma_learner import COMALearner
from .iper import FormDivedeLearner

REGISTRY = {}

REGISTRY["q_learner"] = QLearner
REGISTRY["coma_learner"] = COMALearner

REGISTRY["iper"] = FormDivedeLearner