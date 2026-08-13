import os
import uuid
import streamlit as st
import vertexai
from vertexai import agent_engines

# --- 1. Configuration ---
PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "ssd-instagram-campaign")
LOCATION = os.getenv("CLOUD_RUN_REGION", os.getenv("LOCATION", "us-central1"))
REASONING_ENGINE_ID = os.getenv("AGENT_ENGINE_ID", "8797123814359040000")

vertexai.init(project=PROJECT_ID, location=LOCATION)

st.set_page_config(page_title="Creative Director", page_icon="🎨", layout="wide")

# --- 2. Client Setup ---
@st.cache_resource
def get_remote_agent():
    resource_name = f"projects/{PROJECT_ID}/locations/{LOCATION}/reasoningEngines/{REASONING_ENGINE_ID}"
    return agent_engines.get(resource_name)

agent = get_remote_agent()

# --- 3. Session Logic ---
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

# --- 4. Main UI ---
st.title("🎨 Creative Director Orchestrator")
st.caption(f"Session: `{st.session_state.agent_session_id}` | User: `{st.session_state.user_uuid}`")

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

if prompt := st.chat_input("Submit your campaign brief..."):
    st.chat_message("user").markdown(prompt)
    st.session_state.messages.append({"role": "user", "content": prompt})

    with st.chat_message("assistant"):
        try:
            status_container = st.status("🎨 Orchestrating specialists...", expanded=True)
            text_chunks = []

            response_stream = agent.stream_query(
                user_id=st.session_state.user_uuid,
                session_id=st.session_state.agent_session_id,
                message=prompt,
            )

            placeholder = st.empty()

            for event in response_stream:
                # 1. Plain string event
                if isinstance(event, str):
                    text_chunks.append(event)
                    placeholder.markdown("".join(text_chunks))
                    continue

                # 2. Extract content & parts (supports dict or object attributes)
                content = event.get("content", {}) if isinstance(event, dict) else getattr(event, "content", None)
                parts = []
                if isinstance(content, dict):
                    parts = content.get("parts", [])
                elif hasattr(content, "parts"):
                    parts = content.parts

                for part in parts:
                    part_text = None
                    func_call = None

                    if isinstance(part, dict):
                        part_text = part.get("text")
                        func_call = part.get("function_call")
                    else:
                        part_text = getattr(part, "text", None)
                        func_call = getattr(part, "function_call", None)

                    if func_call:
                        func_name = func_call.get("name") if isinstance(func_call, dict) else getattr(func_call, "name", "specialist")
                        status_container.write(f"🤖 Calling specialist: **{func_name}**...")
                    elif part_text:
                        text_chunks.append(part_text)
                        placeholder.markdown("".join(text_chunks))

                # 3. Fallback extraction if no parts
                if not parts:
                    fallback_text = None
                    if isinstance(event, dict):
                        fallback_text = event.get("text") or event.get("output") or event.get("response")
                    else:
                        fallback_text = getattr(event, "text", None) or getattr(event, "output", None)

                    if isinstance(fallback_text, str) and fallback_text:
                        text_chunks.append(fallback_text)
                        placeholder.markdown("".join(text_chunks))

            full_response = "".join(text_chunks)
            if full_response:
                status_container.update(label="✓ Campaign orchestration complete!", state="complete", expanded=False)
                st.session_state.messages.append({"role": "assistant", "content": full_response})
            else:
                status_container.update(label="⚠️ Orchestrator completed with no direct text response.", state="error", expanded=True)
                st.warning("No text was extracted from the response stream. Check the backend logs for details.")

        except Exception as e:
            st.error(f"Execution Error: {e}")

# Sidebar
if st.sidebar.button("New Campaign"):
    st.session_state.clear()
    st.rerun()
