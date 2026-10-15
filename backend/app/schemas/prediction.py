"""v1训练特征契约，预测不生成缺失的虚拟客户。"""
from typing import Annotated, Literal
from pydantic import BaseModel, Field, StrictInt, StrictFloat
from app.services.data_loader import FEATURE_NAMES, GEOGRAPHY_MAP, GENDER_MAP
import numpy as np


class PredictionFeatures(BaseModel):
    credit_score: Annotated[StrictInt, Field(ge=350, le=850)]
    age: Annotated[StrictInt, Field(ge=18, le=120)]
    tenure: Annotated[StrictInt, Field(ge=0, le=100)]
    balance: Annotated[StrictFloat, Field(ge=0, le=1e12, allow_inf_nan=False)]
    num_products: Annotated[StrictInt, Field(ge=1, le=4)]
    has_credit_card: Annotated[StrictInt, Field(ge=0, le=1)]
    is_active_member: Annotated[StrictInt, Field(ge=0, le=1)]
    estimated_salary: Annotated[StrictFloat, Field(ge=0, le=1e12, allow_inf_nan=False)]
    satisfaction_score: Annotated[StrictInt, Field(ge=1, le=5)]
    points_earned: Annotated[StrictInt, Field(ge=0, le=100000)]
    geography: Literal['France', 'Germany', 'Spain']
    gender: Literal['Male', 'Female']


def feature_vector(data):
    values = PredictionFeatures.model_validate(data).model_dump()
    values['geography'] = GEOGRAPHY_MAP[values['geography']]
    values['gender'] = GENDER_MAP[values['gender']]
    return np.array([[values[key] for key in FEATURE_NAMES]], dtype=float)
