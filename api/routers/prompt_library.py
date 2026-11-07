"""Prompt library API (G24/G25).

Endpoints:
  GET    /api/prompts              — list all prompt names
  GET    /api/prompts/{name}       — get a prompt's definition
  POST   /api/prompts/{name}/render — render a prompt with variables
"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from open_notebook.services.prompt_library import prompt_library

router = APIRouter()


class PromptInfo(BaseModel):
    name: str
    description: str
    variables: list[str]


class RenderRequest(BaseModel):
    variables: Dict[str, Any] = Field(default_factory=dict)


class RenderResponse(BaseModel):
    prompt: str
    rendered: str


@router.get("/prompts", response_model=List[PromptInfo])
async def list_prompts():
    """List all versioned prompts in the library."""
    result: list[PromptInfo] = []
    for name in prompt_library.list():
        prompt = prompt_library.get(name)
        if prompt is not None:
            result.append(
                PromptInfo(
                    name=prompt.name,
                    description=prompt.description or "",
                    variables=prompt.variables,
                )
            )
    return result


@router.get("/prompts/{name}", response_model=PromptInfo)
async def get_prompt(name: str):
    """Get a single prompt's definition."""
    prompt = prompt_library.get(name)
    if prompt is None:
        raise HTTPException(status_code=404, detail=f"Prompt '{name}' not found")
    return PromptInfo(
        name=prompt.name,
        description=prompt.description,
        variables=prompt.variables,
    )


@router.post("/prompts/{name}/render", response_model=RenderResponse)
async def render_prompt(name: str, request: RenderRequest):
    """Render a prompt template with variables."""
    prompt = prompt_library.get(name)
    if prompt is None:
        raise HTTPException(status_code=404, detail=f"Prompt '{name}' not found")
    rendered = prompt_library.render(name, **request.variables)
    if rendered is None:
        raise HTTPException(status_code=400, detail=f"Failed to render prompt '{name}'")
    return RenderResponse(prompt=prompt.template, rendered=rendered)
