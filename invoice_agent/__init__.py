import os

# Keep Pydantic AI's startup banner out of CLI output; must run before pydantic_ai is imported.
os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
