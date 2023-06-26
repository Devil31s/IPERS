from .q_learner import QLearner
from .coma_learner import COMALearner
from .qtran_learner import QLearner as QTranLearner
from .maser_q_learner import maserQLearner
from .q_learner_divide import QDivedeLearner

from .maser_q_divide_learner import maserQDivideLearner
from .maser_zyf import maserQDivideLearnerzyf
from .maser_my import maserQDivideLearner2
from .maser_various import maserQDivideLearnerV
from .maser_new import maserQDivideLearnerNEW

from .maser_formdivide import FormDivedeLearner

REGISTRY = {}

REGISTRY["q_learner"] = QLearner
REGISTRY["coma_learner"] = COMALearner
REGISTRY["qtran_learner"] = QTranLearner
REGISTRY["maser_q_learner"] = maserQLearner
REGISTRY["q_learner_divide"] = QDivedeLearner

REGISTRY["maser_q_divide_learner"] = maserQDivideLearner
REGISTRY["maser_zyf"] = maserQDivideLearnerzyf
REGISTRY["maser_my"] = maserQDivideLearner2
REGISTRY["maser_various"] = maserQDivideLearnerV
REGISTRY["maser_new"] = maserQDivideLearnerNEW

REGISTRY["maser_formdivide"] = FormDivedeLearner