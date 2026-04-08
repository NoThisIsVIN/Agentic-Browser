from pydantic import BaseModel, Field
from typing import Literal


class AgentBrain(BaseModel):
    evaluation_previous_goal: str = Field(
        description=(
            "Evaluate whether the previous next_goal was achieved. Start with Success, Failed, or Unknown, "
            "then briefly explain why."
        )
    )
    memory: str = Field(
        description=(
            "Short running memory of useful facts and progress that should be remembered across steps."
        )
    )
    next_goal: str = Field(
        description="The next immediate goal that should be achieved with the upcoming action."
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

# --- THE VISION TOOL ---
class LookAction(BaseModel):
    action: Literal["look"] = "look"
    reason: str = Field(description="Why you need to see the screen (e.g., 'To read the math formula' or 'To see the image').")
# -----------------------

# --- THE FINAL OUTPUT ---
class AgentOutput(BaseModel):
    thought: str = Field(description="The agent's internal reasoning.")
    current_state: AgentBrain = Field(description="Evaluation, memory, and next-goal state for the current step.")
    command: ClickAction | TypeAction | FinishAction | GotoAction | ReadAction | ScrollAction | PressAction | LookAction
# ------------------------
