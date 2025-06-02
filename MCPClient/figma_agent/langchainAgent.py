# ./adk_agent_samples/mcp_agent/agent.py
import os # Required for path operations
import json
import websocket
import logging
import time
import threading
import uuid
from typing import Dict, Any, Optional, Union
from langchain.agents import AgentExecutor, create_openai_functions_agent
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_google_genai import ChatGoogleGenerativeAI

# Configure logging
logger = logging.getLogger(__name__)

# WebSocket connection variables
ws: Optional[websocket.WebSocketApp] = None
current_channel = None
pending_requests: Dict[str, Dict[str, Any]] = {}
WS_URL = "ws://localhost:3055"

if "GOOGLE_API_KEY" not in os.environ:
    os.environ["GOOGLE_API_KEY"] = getpass.getpass("Enter your Google AI API key: ")


def connect_to_figma(port: int = 3055) -> None:
    """Connect to Figma WebSocket server."""
    global ws, current_channel
    
    # If already connected, do nothing
    if ws and ws.sock and ws.sock.connected:
        logger.info('Already connected to Figma')
        return

    ws_url = f"{WS_URL}:{port}"
    logger.info(f"Connecting to Figma socket server at {ws_url}...")
    
    def on_message(ws, message):
        try:
            json_data = json.loads(message)
            
            # Handle progress updates
            if json_data.get('type') == 'progress_update':
                progress_data = json_data['message']['data']
                request_id = json_data.get('id', '')
                
                if request_id and request_id in pending_requests:
                    request = pending_requests[request_id]
                    
                    # Update last activity timestamp
                    request['last_activity'] = time.time()
                    
                    # Reset the timeout
                    if 'timeout' in request:
                        request['timeout'].cancel()
                    
                    # Create new timeout
                    request['timeout'] = threading.Timer(60.0, lambda: handle_timeout(request_id))
                    request['timeout'].start()
                    
                    # Log progress
                    logger.info(f"Progress update for {progress_data['commandType']}: {progress_data['progress']}% - {progress_data['message']}")
                    
                    # Handle completion
                    if progress_data['status'] == 'completed' and progress_data['progress'] == 100:
                        logger.info(f"Operation {progress_data['commandType']} completed, waiting for final result")
                return
            
            # Handle regular responses
            my_response = json_data['message']
            logger.debug(f"Received message: {json.dumps(my_response)}")
            logger.info(f"myResponse: {json.dumps(my_response)}")
            
            # Handle response to a request
            if my_response.get('id') and my_response['id'] in pending_requests and my_response.get('result'):
                request = pending_requests[my_response['id']]
                if 'timeout' in request:
                    request['timeout'].cancel()
                
                if my_response.get('error'):
                    logger.error(f"Error from Figma: {my_response['error']}")
                    request['reject'](Exception(my_response['error']))
                else:
                    if my_response.get('result'):
                        request['resolve'](my_response['result'])
                
                del pending_requests[my_response['id']]
            else:
                # Handle broadcast messages or events
                logger.info(f"Received broadcast message: {json.dumps(my_response)}")
                
        except Exception as error:
            logger.error(f"Error parsing message: {str(error)}")

    def on_error(ws, error):
        logger.error(f"Socket error: {error}")

    def on_close(websocket, close_status_code, close_msg):
        global ws
        logger.info('Disconnected from Figma socket server')
        ws = None
        
        # Reject all pending requests
        for request_id, request in list(pending_requests.items()):
            if 'timeout' in request:
                request['timeout'].cancel()
            request['reject'](Exception("Connection closed"))
            del pending_requests[request_id]
        
        # Attempt to reconnect
        logger.info('Attempting to reconnect in 2 seconds...')
        threading.Timer(2.0, lambda: connect_to_figma(port)).start()

    def on_open(ws):
        logger.info('Connected to Figma socket server')
        global current_channel
        current_channel = None

    ws = websocket.WebSocketApp(
        ws_url,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
        on_open=on_open
    )
    
    # Start WebSocket connection in a separate thread
    threading.Thread(target=ws.run_forever, daemon=True).start()

def handle_timeout(request_id: str) -> None:
    """Handle request timeout."""
    if request_id in pending_requests:
        logger.error(f"Request {request_id} timed out after extended period of inactivity")
        request = pending_requests[request_id]
        request['reject'](Exception('Request to Figma timed out'))
        del pending_requests[request_id]

def send_command_to_figma(
    command: str,
    params: Dict[str, Any] = None,
    timeout_ms: int = 30000
) -> Any:
    """Send a command to Figma and wait for response.
    
    Args:
        command: The command to send to Figma
        params: Optional parameters for the command
        timeout_ms: Timeout in milliseconds
        
    Returns:
        The response from Figma
        
    Raises:
        Exception: If not connected or command fails
    """
    if params is None:
        params = {}
        
    # If not connected, try to connect first
    if not ws or not ws.sock or not ws.sock.connected:
        connect_to_figma()
        raise Exception("Not connected to Figma. Attempting to connect...")

    # Check if we need a channel for this command
    requires_channel = command != "join"
    if requires_channel and not current_channel:
        raise Exception("Must join a channel before sending commands")

    request_id = str(uuid.uuid4())
    request = {
        "id": request_id,
        "type": "join" if command == "join" else "message",
        "channel": params.get("channel") if command == "join" else current_channel,
        "message": {
            "id": request_id,
            "command": command,
            "params": {
                **params,
                "commandId": request_id  # Include the command ID in params
            }
        }
    }

    # Create a promise-like structure using threading.Event
    event = threading.Event()
    result = {"value": None, "error": None}

    def resolve(value):
        result["value"] = value
        event.set()

    def reject(error):
        result["error"] = error
        event.set()

    # Set timeout for request
    timeout = threading.Timer(timeout_ms / 1000.0, lambda: handle_timeout(request_id))
    timeout.start()

    # Store the promise callbacks to resolve/reject later
    pending_requests[request_id] = {
        "resolve": resolve,
        "reject": reject,
        "timeout": timeout,
        "last_activity": time.time()
    }

    # Send the request
    logger.info(f"Sending command to Figma: {command}")
    logger.debug(f"Request details: {json.dumps(request)}")
    ws.send(json.dumps(request))

    # Wait for response or timeout
    event.wait()
    
    # Clean up timeout
    timeout.cancel()
    
    # Check for error
    if result["error"]:
        raise result["error"]
        
    return result["value"]

@tool
def join_channel(channel_name: str) -> str:
    """Join a specific channel to communicate with Figma.
    
    Args:
        channel_name: The name of the channel to join
        
    Returns:
        str: Status message about the channel join operation
    """
    if not ws or not ws.sock or not ws.sock.connected:
        connect_to_figma()
        return "Not connected to Figma. Attempting to connect..."

    try:
        send_command_to_figma("join", {"channel": channel_name})
        global current_channel
        current_channel = channel_name
        logger.info(f"Joined channel: {channel_name}")
        return f"Successfully joined channel: {channel_name}"
    except Exception as error:
        error_msg = str(error)
        logger.error(f"Failed to join channel: {error_msg}")
        return f"Error joining channel: {error_msg}"

@tool
def get_document_info() -> str:
    """Get detailed information about the current Figma document.
    
    Returns:
        str: JSON string containing the document information
    """
    try:
        result = send_command_to_figma("get_document_info")
        return json.dumps(result, indent=2)
    except Exception as error:
        error_msg = str(error)
        logger.error(f"Error getting document info: {error_msg}")
        return f"Error getting document info: {error_msg}"

# Create the agent
tools = [join_channel, get_document_info]

# prompt = ChatPromptTemplate.from_messages([
#     ("system", """You are an autonomous UX designer. You can perform CRUD operations on figma components. 
#     You can convert the user requirements into beautiful and standardized figma updations. 
#     Talk to figma by joining channel 4gr50b1k"""),
#     MessagesPlaceholder(variable_name="messages"),
#     ("human", "{input}"),
#     MessagesPlaceholder(variable_name="agent_scratchpad"),
# ])
prompt = ChatPromptTemplate([
    ("system", """You are an autonomous UX designer. You can perform CRUD operations on figma components. 
    You can convert the user requirements into beautiful and standardized figma updations. 
    Talk to figma by joining channel 4gr50b1k"""),
])

llm = ChatGoogleGenerativeAI(
    model="gemini-2.0-flash",
    temperature=0,
    max_tokens=None,
    timeout=None,
    max_retries=2,
)

agent_executor = All(
    model=llm,
    tools=tools,
    prompt=prompt
)

# Function to run the agent
def run_agent(user_input: str) -> str:
    """Run the agent with user input.
    
    Args:
        user_input: The user's input message
        
    Returns:
        str: The agent's response
    """
    try:
        response = agent_executor.invoke({"input": {"contents": [user_input]}})
        return response["output"]
    except Exception as e:
        return f"Error running agent: {str(e)}"
    
print(run_agent("Hello"))