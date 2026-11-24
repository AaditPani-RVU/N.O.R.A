"""Story watch by voice (NORA_ASSIST_PLAN D)."""
from __future__ import annotations

from nora.command_engine import register


@register(
    "watch_story",
    sig="watch_story(topic: str)",
    description="Follow a news story and tell the user when it changes: 'keep an eye on the "
                "Strait of Hormuz', 'follow the Iran US talks', 'track Indian cricket'",
    category="news",
)
def watch_story(topic: str) -> str:
    from nora import story_watch
    return story_watch.watch(topic)


@register(
    "unwatch_story",
    sig="unwatch_story(topic: str)",
    description="Stop following a story",
    category="news",
)
def unwatch_story(topic: str) -> str:
    from nora import story_watch
    return story_watch.unwatch(topic)


@register(
    "story_update",
    sig="story_update(topic: str = '')",
    description="What changed on a story the user follows, since NORA last said: "
                "'what's new on Hormuz', 'any update on my stories'. Only for watched stories",
    category="news",
)
def story_update(topic: str = "") -> str:
    from nora import story_watch
    return story_watch.update(topic)


@register(
    "list_stories",
    sig="list_stories()",
    description="The stories NORA is following for the user",
    category="news",
)
def list_stories() -> str:
    from nora import story_watch
    return story_watch.listing()
