from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from .models import Question


QUESTION_FILE = Path(__file__).parent / "content" / "questions.json"


@lru_cache(maxsize=1)
def load_questions() -> list[Question]:
    with QUESTION_FILE.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return [Question.model_validate(item) for item in payload]


def question_map() -> dict[str, Question]:
    return {question.id: question for question in load_questions()}
