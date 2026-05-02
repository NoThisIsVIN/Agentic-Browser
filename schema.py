from pydantic import BaseModel, Field
from typing import Literal, Union, Annotated


class AgentBrain(BaseModel):
    evaluation_previous_goal: str = Field(
        description="Success/Failed/Unknown + max 5 words why."
    )
    memory: str = Field(
        description="Short bullet-style notes of progress so far."
    )
    next_goal: str = Field(
        description="ONE short phrase for the immediate next goal."
    )


class ClickAction(BaseModel):
    action: Literal["click"] = "click"
    element_id: int = Field(description="The numeric ID of the element to click.")

class TypeAction(BaseModel):
    action: Literal["type"] = "type"
    element_id: int = Field(description="The numeric ID of the input field.")
    text: str = Field(description="The text to type.")

class FinishAction(BaseModel):
    action: Literal["finish"] = "finish"
    success: bool = Field(description="True if complete, False if impossible.")
    reason: str = Field(description="A brief explanation or the extracted final text.")

class GotoAction(BaseModel):
    action: Literal["goto"] = "goto"
    url: str = Field(description="The full URL to navigate to.")

class ReadAction(BaseModel):
    action: Literal["read"] = "read"
    reason: str = Field(description="Why you need a deep scan of the page paragraphs.")

class ScrollAction(BaseModel):
    action: Literal["scroll"] = "scroll"
    direction: Literal["down", "up"] = Field(description="The direction to scroll the page.")

class PressAction(BaseModel):
    action: Literal["press"] = "press"
    key: str = Field(description="The keyboard key to press (e.g., 'Escape', 'Enter', 'Tab').")

class WaitAction(BaseModel):
    action: Literal["wait"] = "wait"
    reason: str = Field(description="Why you need to wait for the page to settle.")

ActionType = Union[
    ClickAction, TypeAction, FinishAction, GotoAction,
    ReadAction, ScrollAction, PressAction, WaitAction,
]


class AgentOutput(BaseModel):
    current_state: AgentBrain = Field(description="Evaluation, memory, and next-goal.")
    actions: list[ActionType] = Field(
        description="1-5 browser actions to execute in sequence. Combine related steps (e.g. type + press Enter).",
    )
