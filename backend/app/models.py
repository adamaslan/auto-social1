from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field


class CampaignRequest(BaseModel):
    product: str = Field(default="Autonomous Growth Agent")
    event: str
    audience: str = Field(default="B2B SaaS founders and operators")
    launch_date: str = Field(default="next week")
    channels: list[str] = Field(default_factory=lambda: ["instagram", "telegram", "bluesky", "x", "press"])


class CampaignPack(BaseModel):
    run_id: str
    automation_state: str = Field(default="drafted")
    risk_level: str = Field(default="medium")
    campaign_name: str
    angle: str
    posts: dict[str, str]
    image_prompts: list[str]
    press_pitch: str
    calendar: list[str]
    publish_policy: dict[str, str]


Channel = Literal["instagram", "telegram", "bluesky"]


class PublishRequest(BaseModel):
    channel: Channel
    text: str
    campaign_name: str = Field(default="Untitled Campaign")
    image_prompt: str | None = None
    local_image_path: str | None = None
    media_url: str | None = None
    alt_text: str | None = None
    link_url: str | None = None
    dry_run: bool = Field(default=True)


class PublishResult(BaseModel):
    publish_log_id: str
    channel: Channel
    status: str
    dry_run: bool
    external_id: str = ""
    error: str = ""
    rate_limit: str
    retryable: bool = False
    next_action: str = ""
    diagnostics: dict = Field(default_factory=dict)
    payload: dict


class ChannelAdapterStatus(BaseModel):
    channel: Channel
    configured: bool
    mode: str
    rate_limit: str
    supports_autopublish: bool
    protocol: str
    required_config: list[str] = Field(default_factory=list)
    missing_config: list[str] = Field(default_factory=list)
    next_action: str = ""


class Story(BaseModel):
    url: str
    title: str
    summary: str = ""
    source: str = "manual"
    published_at: str = ""


class ArticleSection(BaseModel):
    heading: str
    paragraphs: list[str] = Field(default_factory=list)


class Article(BaseModel):
    title: str
    slug: str
    subtitle: str = ""
    og_description: str = ""
    keywords: list[str] = Field(default_factory=list)
    sections: list[ArticleSection] = Field(default_factory=list)
    image_prompt: str = ""


class AiNewsRunRequest(BaseModel):
    dry_run: bool | None = None
    publish: bool = Field(default=False)
    sources: list[str] | None = None
    story_url: str | None = None
    story_title: str | None = None
    story_summary: str | None = None
    source_name: str = Field(default="manual")


class AiNewsRunResult(BaseModel):
    run_id: str
    status: str
    story_url: str = ""
    story_title: str = ""
    article_slug: str = ""
    route_file: str = ""
    image_filename: str = ""
    image_provider_used: str = ""
    caption: str = ""
    publish_log_id: str = ""
    media_id: str = ""
    dry_run: bool = True
    error: str = ""


@dataclass
class ImageValidation:
    width: int
    height: int
    ratio: float
    format: str
    aspect_ok: bool
    dimension_ok: bool
    is_likely_blank: bool
    mean_color: list[float] = field(default_factory=list)
    color_stddev: list[float] = field(default_factory=list)


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    kind: str
    status: str
    created_at: str
    completed_at: str | None
    duration_ms: int | None

    # Inputs
    sources_used: list[str] = field(default_factory=list)
    candidates_count: int = 0
    candidates_file: str = "candidates.json"

    # Decisions
    story_url: str | None = None
    story_title: str | None = None
    story_source: str | None = None
    picker_provider: str | None = None
    picker_fallback_used: bool = False

    # Article
    article_slug: str | None = None
    article_provider: str | None = None
    article_model: str | None = None
    article_prompt_tokens: int | None = None
    article_completion_tokens: int | None = None
    article_attempts: int = 1
    article_file: str | None = None

    # Image
    image_provider_attempted: str = "gemini"
    image_provider_used: str | None = None
    image_prompt: str | None = None
    image_style_suffix: str = ""
    image_filename: str | None = None
    image_bytes: int | None = None
    image_width: int | None = None
    image_height: int | None = None
    image_aspect_ratio: float | None = None

    # Publish
    publish_called: bool = False
    publish_log_id: str | None = None
    media_container_id: str | None = None
    media_external_id: str | None = None
    media_public_url: str | None = None
    publish_response_file: str | None = None

    # Errors
    failed_step: str | None = None
    error_class: str | None = None
    error_message: str | None = None
    retryable: bool = False

    # Cost accounting
    llm_total_tokens: int = 0
    external_call_count: int = 0
    total_bytes_downloaded: int = 0
