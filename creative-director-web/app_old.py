import streamlit as st
import vertexai
import uuid
import json
from google.cloud import aiplatform_v1beta1
from google.protobuf import struct_pb2
from google.protobuf.json_format import MessageToDict

# --- Configuration ---
PROJECT_ID = "ssd-instagram-campaign"
LOCATION = "us-central1"
REASONING_ENGINE_ID = "8797123814359040000" 

vertexai.init(project=PROJECT_ID, location=LOCATION)

st.set_page_config(page_title="Creative Director", page_icon="🎨")
st.title("🎨 Creative Director Orchestrator")

def dict_to_proto_struct(d):
    s = struct_pb2.Struct()
    s.update(d)
    return s

def parse_response(response):
    """Safely converts Protobuf to Dict, handling the 'upb' environment."""
    res_pb = getattr(response, "_pb", response)
    return MessageToDict(res_pb, preserving_proto_field_name=True)

@st.cache_resource
def get_execution_client():
    return aiplatform_v1beta1.ReasoningEngineExecutionServiceClient(
        client_options={"api_endpoint": f"{LOCATION}-aiplatform.googleapis.com"}
    )

client = get_execution_client()
resource_name = f"projects/{PROJECT_ID}/locations/{LOCATION}/reasoningEngines/{REASONING_ENGINE_ID}"

# --- Session Management ---
if "user_uuid" not in st.session_state:
    st.session_state.user_uuid = str(uuid.uuid4())

if "agent_session_id" not in st.session_state:
    with st.spinner("Connecting to Creative Director..."):
        try:
            request = aiplatform_v1beta1.QueryReasoningEngineRequest(
                name=resource_name,
                input=dict_to_proto_struct({"user_id": st.session_state.user_uuid}),
                class_method="create_session"
            )
            response = client.query_reasoning_engine(request=request)
            data = parse_response(response)
            
            # ADK App create_session returns the session object in 'output'
            sid = data.get("output", {}).get("id")
            if sid:
                st.session_state.agent_session_id = sid
                st.session_state.messages = []
            else:
                st.error("Failed to create session. Backend returned empty ID.")
                st.json(data)
                st.stop()
        except Exception as e:
            st.error(f"Init Error: {e}")
            st.stop()

# --- Chat UI ---
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

if prompt := st.chat_input("Ask the Creative Director..."):
    st.chat_message("user").markdown(prompt)
    st.session_state.messages.append({"role": "user", "content": prompt})

    with st.chat_message("assistant"):
        msg_placeholder = st.empty()
        
        # Extended timeout because coordinating 5 agents can take time
        with st.spinner("Orchestrating specialists (Copywriter, Designer, etc.)..."):
            try:
                payload = {
                    "session_id": st.session_state.agent_session_id,
                    "user_id": st.session_state.user_uuid,
                    "question": prompt
                }
                
                request = aiplatform_v1beta1.QueryReasoningEngineRequest(
                    name=resource_name,
                    input=dict_to_proto_struct(payload),
                    class_method="get_session"
                )

                # Execute with high timeout for complex orchestration
                response = client.query_reasoning_engine(request=request, timeout=300)
                full_dict = parse_response(response)
                output_data = full_dict.get("output", {})

                # --- SEARCH LOGIC FOR THE ANSWER ---
                final_answer = ""
                
                # 1. Check 'events' (The most likely place in ADK App)
                events = output_data.get("events", [])
                if events:
                    # Reverse search for the last model/assistant message
                    for event in reversed(events):
                        role = str(event.get("role", "")).lower()
                        if role in ["model", "assistant", "agent", "ai"]:
                            final_answer = event.get("text") or event.get("content")
                            if final_answer: break

                # 2. Check top-level 'output' (If the method returns a direct string)
                if not final_answer:
                    # Some ADK versions put the latest response in a top-level 'output' key
                    # inside the session object's output.
                    raw_out = output_data.get("output")
                    if isinstance(raw_out, str):
                        final_answer = raw_out
                    elif isinstance(raw_out, dict):
                        final_answer = raw_out.get("text") or raw_out.get("content")

                # 3. Check 'state'
                if not final_answer:
                    state = output_data.get("state", {})
                    final_answer = state.get("output") or state.get("text")

                # 4. Final Fallback - if we found nothing, show the raw JSON
                if not final_answer or final_answer == "{}":
                    st.warning("No text extracted. Check the raw session data below.")
                    with st.expander("Raw Session JSON"):
                        st.json(full_dict)
                    final_answer = "The Creative Director processed the request but returned no text."

                msg_placeholder.markdown(final_answer)
                st.session_state.messages.append({"role": "assistant", "content": final_answer})

            except Exception as e:
                st.error(f"Error: {e}")

# Sidebar
with st.sidebar:
    st.write(f"Session ID: `{st.session_state.agent_session_id}`")
    if st.button("Reset Campaign"):
        if "agent_session_id" in st.session_state:
            del st.session_state.agent_session_id
        st.rerun()