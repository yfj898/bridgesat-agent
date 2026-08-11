from app.engine import adapt, score_diagnostic
from app.models import AdaptRequest, DiagnosticAnswer, Question, Skill, Student


def make_student() -> Student:
    return Student(
        id="student-1",
        name="Test Student",
        daily_minutes=20,
        target_score=1200,
        mastery={skill: 0.5 for skill in Skill},
    )


def test_diagnostic_prioritizes_lowest_skill() -> None:
    result = score_diagnostic(
        make_student(),
        [
            DiagnosticAnswer(question_id="linear-001", selected_answer="3"),
            DiagnosticAnswer(question_id="ratio-001", selected_answer="4"),
            DiagnosticAnswer(
                question_id="reading-001",
                selected_answer="Mina expected the weather might change.",
            ),
        ],
    )

    assert result.weakest_skills[0] == Skill.LINEAR_EQUATIONS
    assert sum(item.minutes for item in result.plan) == 20


def test_diagnostic_prioritizes_sampled_expansion_skill_over_untested_legacy_skill(
    monkeypatch,
) -> None:
    student = make_student()
    student.mastery[Skill.READING_INFERENCE] = 0.0
    question = Question(
        id="math.inequalities.001",
        skill=Skill.INEQUALITIES,
        difficulty=1,
        prompt="Which integer satisfies the inequality?",
        choices=["3", "4"],
        answer="4",
        hints=["Check the boundary."],
        explanation="The boundary is excluded.",
    )
    monkeypatch.setattr("app.engine.question_map", lambda: {question.id: question})

    result = score_diagnostic(
        student,
        [
            DiagnosticAnswer(
                question_id="math.inequalities.001",
                selected_answer="not-the-correct-choice",
            )
        ],
    )

    assert result.weakest_skills[0] == Skill.INEQUALITIES


def test_repeated_errors_insert_micro_lesson() -> None:
    result = adapt(
        0.55,
        AdaptRequest(
            student_id="student-1",
            skill=Skill.LINEAR_EQUATIONS,
            was_correct=False,
            consecutive_skill_errors=2,
            minutes_remaining=10,
        ),
    )

    assert result.action == "insert_micro_lesson"
    assert result.next_difficulty_delta == -1


def test_low_time_ends_with_review() -> None:
    result = adapt(
        0.7,
        AdaptRequest(
            student_id="student-1",
            skill=Skill.RATIOS,
            was_correct=True,
            minutes_remaining=2,
        ),
    )

    assert result.action == "end_with_review"
