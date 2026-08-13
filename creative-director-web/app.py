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
        def stream_agent_response():
            response_stream = agent.stream_query(
                user_id=st.session_state.user_uuid,
                session_id=st.session_state.agent_session_id,
                message=prompt,
            )
            for event in response_stream:
                if isinstance(event, str):
                    yield event
                elif isinstance(event, dict):
                    # Handle text chunks in parts
                    parts = event.get("content", {}).get("parts", []) if isinstance(event.get("content"), dict) else []
                    for part in parts:
                        if isinstance(part, dict) and "text" in part:
                            yield part["text"]
                    if not parts:
                        text = event.get("text") or event.get("output") or event.get("response")
                        if isinstance(text, str):
                            yield text

        try:
            full_response = st.write_stream(stream_agent_response)
            st.session_state.messages.append({"role": "assistant", "content": full_response})
        except Exception as e:
            st.error(f"Execution Error: {e}")

# Sidebar
if st.sidebar.button("New Campaign"):
    st.session_state.clear()
    st.rerun()
