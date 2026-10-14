from __future__ import annotations

import os
import re
import uuid
import streamlit as st
import vertexai
from vertexai import agent_engines

# --- 1. Configuration ---
PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "ssd-instagram-campaign")
LOCATION = os.getenv("CLOUD_RUN_REGION", os.getenv("LOCATION", "us-central1"))
REASONING_ENGINE_ID = os.getenv("AGENT_ENGINE_ID", "9218421485750714368")

SUPPORTED_GEMINI_VERSIONS = ("3.5", "3.7", "3.8", "4.0")
GEMINI_VERSION_MODELS = {
    "3.5": "gemini-3.5-flash",
    "3.7": "gemini-3.7-flash",
    "3.8": "gemini-3.8-flash",
    "4.0": "gemini-4.0-flash",
}
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"

IMAGE_MIME_MAP = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
}

GCS_URI_REGEX = re.compile(r"gs://[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]/[^\s\"'<>)\]`]+")
GEMINI_VERSION_REGEX = re.compile(r"(?:^|gemini-)(\d+\.\d+)(?:$|[-/])")


def extract_gemini_version(model_or_version: str | None) -> str | None:
    """Extract the major.minor Gemini version (e.g. '3.7', '3.8', '4.0') from a model or version string."""
    if not model_or_version or not isinstance(model_or_version, str):
        return None
    cleaned = model_or_version.strip().lower()
    if re.fullmatch(r"\d+\.\d+", cleaned):
        return cleaned
    match = GEMINI_VERSION_REGEX.search(cleaned)
    return match.group(1) if match else None


def is_supported_gemini_version(model_or_version: str | None) -> bool:
    """Return True if the model or version string belongs to a supported Gemini version."""
    version = extract_gemini_version(model_or_version)
    return version in SUPPORTED_GEMINI_VERSIONS


def resolve_gemini_model(model_or_version: str | None = None) -> str:
    """
    Resolve and validate a Gemini model identifier across versions (3.5, 3.7, 3.8, 4.0).

    Accepts a shorthand version ('3.7', '3.8', '4.0') or a full model name
    ('gemini-3.7-flash', 'gemini-3.8-pro', 'gemini-4.0-flash', etc.).
    Falls back to GEMINI_MODEL or GEMINI_VERSION environment variables, then DEFAULT_GEMINI_MODEL.
    """
    raw = (
        model_or_version
        if model_or_version is not None
        else (os.getenv("GEMINI_MODEL") or os.getenv("GEMINI_VERSION") or DEFAULT_GEMINI_MODEL)
    )
    cleaned = raw.strip()
    if not cleaned:
        cleaned = DEFAULT_GEMINI_MODEL

    if cleaned in GEMINI_VERSION_MODELS:
        return GEMINI_VERSION_MODELS[cleaned]

    version = extract_gemini_version(cleaned)
    if version not in SUPPORTED_GEMINI_VERSIONS:
        raise ValueError(
            f"Unsupported Gemini model/version '{raw}'. "
            f"Supported versions: {', '.join(SUPPORTED_GEMINI_VERSIONS)}"
        )

    if cleaned.lower().startswith("gemini-"):
        return cleaned.lower()
    return GEMINI_VERSION_MODELS.get(version, f"gemini-{version}-flash")


def is_valid_gcs_uri(uri: str | None) -> bool:
    """Validate that a string is a well-formed gs://bucket/object_path URI."""
    if not uri or not isinstance(uri, str) or not uri.startswith("gs://"):
        return False
    without_prefix = uri[len("gs://"):]
    if "/" not in without_prefix:
        return False
    bucket_name, blob_path = without_prefix.split("/", 1)
    if not bucket_name or not blob_path:
        return False
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]", bucket_name):
        return False
    if any(ch.isspace() for ch in blob_path) or blob_path.startswith("/"):
        return False
    return True


def parse_gcs_uri(gcs_uri: str) -> tuple[str, str]:
    """Parse a gs://bucket/blob_path URI into (bucket_name, blob_path)."""
    if not is_valid_gcs_uri(gcs_uri):
        raise ValueError(f"Invalid GCS URI: {gcs_uri!r}")
    without_prefix = gcs_uri[len("gs://"):]
    bucket_name, blob_path = without_prefix.split("/", 1)
    return bucket_name, blob_path


def infer_image_mime_type(uri: str | None, explicit_mime_type: str | None = None) -> str | None:
    """Return an image/* MIME type from explicit_mime_type or inferred from the URI file extension."""
    if isinstance(explicit_mime_type, str) and explicit_mime_type.startswith("image/"):
        return explicit_mime_type
    if not uri or not isinstance(uri, str):
        return None
    clean_uri = uri.split("?", 1)[0].split("#", 1)[0]
    if "." not in clean_uri.rsplit("/", 1)[-1]:
        return None
    ext = clean_uri.rsplit(".", 1)[-1].lower()
    return IMAGE_MIME_MAP.get(ext)


def extract_gcs_uris_from_text(text: str | None, image_only: bool = True) -> list[str]:
    """Extract valid, deduplicated gs:// URIs from text/markdown responses."""
    if not text or not isinstance(text, str):
        return []
    uris: list[str] = []
    seen: set[str] = set()
    for match in GCS_URI_REGEX.finditer(text):
        candidate = match.group(0).rstrip(".,;:!?)]}`>")
        if not is_valid_gcs_uri(candidate):
            continue
        if image_only and not infer_image_mime_type(candidate):
            continue
        if candidate not in seen:
            seen.add(candidate)
            uris.append(candidate)
    return uris


def _get_field(obj, *names, default=None):
    """Retrieve the first matching key or attribute from a dict or object."""
    if obj is None:
        return default
    for name in names:
        if isinstance(obj, dict) and name in obj and obj[name] is not None:
            return obj[name]
        if not isinstance(obj, dict):
            val = getattr(obj, name, None)
            if val is not None:
                return val
    return default


def extract_parts_from_event(event) -> list:
    """
    Extract content parts from a streaming event across Gemini 3.7, 3.8, and 4.0 formats.
    Supports top-level content.parts as well as candidates[0].content.parts.
    """
    content = _get_field(event, "content")
    if content is None:
        candidates = _get_field(event, "candidates")
        if candidates and len(candidates) > 0:
            content = _get_field(candidates[0], "content")

    if content is None:
        return []

    parts = _get_field(content, "parts", default=[])
    return list(parts) if parts else []


def process_stream_events(
    response_stream,
    status_container=None,
    placeholder=None,
    image_renderer=None,
) -> tuple[str, list[dict]]:
    """
    Consume an agent response stream and extract text and image assets.

    Supports response shapes emitted across Gemini 3.5, 3.7, 3.8, and 4.0:
    - Plain string events
    - Dict and object events (top-level content.parts and candidates[0].content.parts)
    - snake_case and camelCase part fields (function_call/functionCall, inline_data/inlineData, file_data/fileData)
    - GCS (gs://) URIs in both file_data parts and text/markdown responses
    """
    text_chunks: list[str] = []
    image_parts_captured: list[dict] = []
    seen_gcs_uris: set[str] = set()

    def _capture_gcs_image(uri: str):
        if uri in seen_gcs_uris:
            return
        seen_gcs_uris.add(uri)
        if image_renderer is not None:
            image_renderer(uri, caption="Generated Asset (GCS)")
        image_parts_captured.append({
            "role": "assistant",
            "content": uri,
            "is_image": True,
            "caption": "Generated Asset (GCS)",
        })

    def _append_text_chunk(chunk: str):
        text_chunks.append(chunk)
        accumulated = "".join(text_chunks)
        if placeholder is not None:
            placeholder.markdown(accumulated)
        for gcs_uri in extract_gcs_uris_from_text(accumulated, image_only=True):
            _capture_gcs_image(gcs_uri)

    for event in response_stream:
        # 1. Plain string event
        if isinstance(event, str):
            _append_text_chunk(event)
            continue

        # 2. Extract content & parts (supports dict, object attributes, or candidates[0].content)
        parts = extract_parts_from_event(event)

        for part in parts:
            is_thought = _get_field(part, "thought", default=False)
            if is_thought is True:
                continue

            part_text = _get_field(part, "text")
            func_call = _get_field(part, "function_call", "functionCall")
            inline_data = _get_field(part, "inline_data", "inlineData")
            file_data = _get_field(part, "file_data", "fileData")

            if func_call:
                func_name = _get_field(func_call, "name", default="specialist")
                if status_container is not None:
                    status_container.write(f"🤖 Calling specialist: **{func_name}**...")

            elif part_text:
                _append_text_chunk(part_text)

            # Handle raw base64/bytes inline images
            elif inline_data:
                mime_type = _get_field(inline_data, "mime_type", "mimeType")
                data = _get_field(inline_data, "data")
                if mime_type and mime_type.startswith("image/") and data is not None:
                    if image_renderer is not None:
                        image_renderer(data, caption="Generated Asset")
                    image_parts_captured.append({
                        "role": "assistant",
                        "content": data,
                        "is_image": True,
                        "caption": "Generated Asset",
                    })

            # Handle Cloud Storage (gs://) or external URI images
            elif file_data:
                file_uri = _get_field(file_data, "file_uri", "fileUri")
                raw_mime_type = _get_field(file_data, "mime_type", "mimeType")
                mime_type = infer_image_mime_type(file_uri, raw_mime_type)
                if file_uri and mime_type and mime_type.startswith("image/"):
                    if file_uri.startswith("gs://") and not is_valid_gcs_uri(file_uri):
                        continue
                    _capture_gcs_image(file_uri)

        # 3. Fallback extraction if no parts
        if not parts:
            fallback_text = _get_field(event, "text", "output", "response")
            if isinstance(fallback_text, str) and fallback_text:
                _append_text_chunk(fallback_text)

    full_response = "".join(text_chunks)
    return full_response, image_parts_captured


# --- 2. Client Setup ---
@st.cache_resource
def get_remote_agent(
    project_id: str = PROJECT_ID,
    location: str = LOCATION,
    reasoning_engine_id: str = REASONING_ENGINE_ID,
):
    resource_name = f"projects/{project_id}/locations/{location}/reasoningEngines/{reasoning_engine_id}"
    return agent_engines.get(resource_name)


def init_session_state(agent):
    """Initialize Streamlit session state and create a remote agent session if needed."""
    if "user_uuid" not in st.session_state:
        st.session_state.user_uuid = str(uuid.uuid4())

    if "messages" not in st.session_state:
        st.session_state.messages = []

    if "agent_session_id" not in st.session_state:
        with st.spinner("🎨 Initializing Creative Director..."):
            try:
                session = agent.create_session(user_id=st.session_state.user_uuid)
                sid = session.get("id") if isinstance(session, dict) else getattr(session, "id", None)
                if sid:
                    st.session_state.agent_session_id = str(sid)
                else:
                    st.session_state.agent_session_id = str(session)
            except Exception as e:
                st.error(f"Init Error: {e}")
                st.stop()


def run_app():
    """Main Streamlit application entry point."""
    gemini_model = resolve_gemini_model()
    vertexai.init(project=PROJECT_ID, location=LOCATION)

    st.set_page_config(page_title="Creative Director", page_icon="🎨", layout="wide")

    agent = get_remote_agent()

    # --- 3. Session Logic ---
    init_session_state(agent)

    # --- 4. Main UI ---
    st.title("🎨 Creative Director Orchestrator")
    st.caption(
        f"Session: `{st.session_state.agent_session_id}` | "
        f"User: `{st.session_state.user_uuid}` | "
        f"Model: `{gemini_model}`"
    )

    # Render historical messages
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            if message.get("is_image"):
                st.image(message["content"], caption=message.get("caption", "Generated Asset"))
            else:
                st.markdown(message["content"])

    if prompt := st.chat_input("Submit your campaign brief..."):
        st.chat_message("user").markdown(prompt)
        st.session_state.messages.append({"role": "user", "content": prompt})

        with st.chat_message("assistant"):
            try:
                status_container = st.status("🎨 Orchestrating specialists...", expanded=True)
                response_stream = agent.stream_query(
                    user_id=st.session_state.user_uuid,
                    session_id=st.session_state.agent_session_id,
                    message=prompt,
                )

                placeholder = st.empty()
                full_response, image_parts_captured = process_stream_events(
                    response_stream=response_stream,
                    status_container=status_container,
                    placeholder=placeholder,
                    image_renderer=st.image,
                )

                if full_response or image_parts_captured:
                    status_container.update(label="✓ Campaign orchestration complete!", state="complete", expanded=False)

                    # Persist text response
                    if full_response:
                        st.session_state.messages.append({"role": "assistant", "content": full_response})

                    # Persist image responses
                    for img_msg in image_parts_captured:
                        st.session_state.messages.append(img_msg)
                else:
                    status_container.update(label="⚠️ Orchestrator completed with no direct response.", state="error", expanded=True)
                    st.warning("No text or media was extracted from the response stream.")

            except Exception as e:
                st.error(f"Execution Error: {e}")

    # Sidebar
    if st.sidebar.button("New Campaign"):
        st.session_state.clear()
        st.rerun()


if __name__ == "__main__":
    run_app()
