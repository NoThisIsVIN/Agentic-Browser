from pydantic import BaseModel, Field
from typing import Literal, Optional

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

# --- NEW TOOL ---
class ReadAction(BaseModel):
    action: Literal["read"] = "read"
    reason: str = Field(description="Why you need a deep scan of the page paragraphs.")
# ----------------

class AgentOutput(BaseModel):
    thought: str = Field(description="The agent's internal reasoning.")
    # --- ADD ReadAction to the union ---
    command: ClickAction | TypeAction | FinishAction | GotoAction | ReadAction