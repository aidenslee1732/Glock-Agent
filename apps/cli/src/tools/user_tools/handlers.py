"""User interaction tool handlers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Optional


@dataclass
class QuestionOption:
    """An option for a question."""
    label: str
    description: str = ""


@dataclass
class Question:
    """A structured question to ask the user."""
    question: str
    header: str  # Short label (max 12 chars)
    options: list[QuestionOption]
    multi_select: bool = False


@dataclass
class QuestionResult:
    """Result from a user question."""
    question: str
    selected: list[str]  # Selected option labels
    custom_text: Optional[str] = None  # If "Other" was selected


# Type alias for the callback function
QuestionCallback = Callable[
    [list[Question]],
    Coroutine[Any, Any, list[QuestionResult]]
]

# Global callback for UI to handle questions
_question_callback: Optional[QuestionCallback] = None


def set_question_callback(callback: Optional[QuestionCallback]) -> None:
    """Set the callback function for asking user questions.

    This should be called by the TUI to register its question handler.

    Args:
        callback: Async function that takes questions and returns results
    """
    global _question_callback
    _question_callback = callback


async def ask_user_question_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Ask the user structured questions.

    This tool allows gathering user preferences, clarifying instructions,
    or getting decisions on implementation choices.

    Args:
        args: Dictionary containing:
            - questions: List of question objects, each with:
                - question: The complete question text
                - header: Short label (max 12 chars) for chip/tag
                - options: List of option objects with label and description
                - multiSelect: Allow multiple selections (default False)
            - answers: Optional pre-filled answers (for re-execution)
            - metadata: Optional metadata for tracking

    Returns:
        Dictionary with user's answers
    """
    questions_data = args.get("questions", [])
    pre_answers = args.get("answers", {})

    if not questions_data:
        return {
            "status": "error",
            "error": "questions array is required",
        }

    if len(questions_data) > 4:
        return {
            "status": "error",
            "error": "Maximum 4 questions allowed per call",
        }

    # Parse questions
    questions = []
    for i, q_data in enumerate(questions_data):
        if not q_data.get("question"):
            return {
                "status": "error",
                "error": f"Question {i+1} is missing 'question' field",
            }

        if not q_data.get("header"):
            return {
                "status": "error",
                "error": f"Question {i+1} is missing 'header' field",
            }

        options_data = q_data.get("options", [])
        if len(options_data) < 2 or len(options_data) > 4:
            return {
                "status": "error",
                "error": f"Question {i+1} must have 2-4 options",
            }

        options = [
            QuestionOption(
                label=opt.get("label", ""),
                description=opt.get("description", ""),
            )
            for opt in options_data
        ]

        questions.append(Question(
            question=q_data["question"],
            header=q_data["header"][:12],  # Enforce max length
            options=options,
            multi_select=q_data.get("multiSelect", False),
        ))

    # If callback is set, use it (interactive mode)
    if _question_callback:
        try:
            results = await _question_callback(questions)

            # Format results
            answers = {}
            for result in results:
                if result.custom_text:
                    answers[result.question] = result.custom_text
                elif result.selected:
                    if len(result.selected) == 1:
                        answers[result.question] = result.selected[0]
                    else:
                        answers[result.question] = result.selected

            return {
                "status": "success",
                "answers": answers,
            }

        except asyncio.CancelledError:
            return {
                "status": "cancelled",
                "error": "Question was cancelled by user",
            }
        except Exception as e:
            return {
                "status": "error",
                "error": f"Failed to get user input: {str(e)}",
            }

    # If pre-answers provided, use them
    if pre_answers:
        return {
            "status": "success",
            "answers": pre_answers,
        }

    # No callback and no pre-answers - return question details for manual handling
    return {
        "status": "pending",
        "message": "Awaiting user input",
        "questions": [
            {
                "question": q.question,
                "header": q.header,
                "options": [
                    {"label": opt.label, "description": opt.description}
                    for opt in q.options
                ],
                "multiSelect": q.multi_select,
            }
            for q in questions
        ],
    }
