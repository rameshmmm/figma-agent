# ./adk_agent_samples/figma_agent/agent.py
import os  # Required for path operations
import json
import websocket
import logging
import time
import threading
import uuid
from typing import Dict, Any, Optional, Union
from google.adk.agents import LlmAgent
from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset, StdioServerParameters

# Configure logging
logger = logging.getLogger(__name__)

# WebSocket connection variables
ws: Optional[websocket.WebSocketApp] = None
current_channel = None
pending_requests: Dict[str, Dict[str, Any]] = {}
WS_URL = "ws://localhost"

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
    params: Optional[Dict[str, Any]] = None,
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

def join_channel(channel_name: str) -> Dict[str, Any]:
    """Join a specific channel to communicate with Figma.

    Args:
        channel_name: The name of the channel to join

    Returns:
        Dict containing status and message

    Raises:
        Exception: If connection fails or channel join fails
    """
    if not ws or not ws.sock or not ws.sock.connected:
        raise Exception("Not connected to Figma")

    try:
        send_command_to_figma("join", {"channel": channel_name})
        global current_channel
        current_channel = channel_name
        logger.info(f"Joined channel: {channel_name}")
        return {
            "status": "success",
            "message": f"Successfully joined channel: {channel_name}"
        }
    except Exception as error:
        error_msg = str(error)
        logger.error(f"Failed to join channel: {error_msg}")
        return {
            "status": "error",
            "message": f"Error joining channel: {error_msg}"
        }

def get_document_info() -> Dict[str, Any]:
    """Get detailed information about the current Figma document.

    Returns:
        Dict containing the document information or error message
    """
    try:
        result = send_command_to_figma("get_document_info")
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(result, indent=2)
                }
            ]
        }
    except Exception as error:
        error_msg = str(error)
        logger.error(f"Error getting document info: {error_msg}")
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Error getting document info: {error_msg}"
                }
            ]
        }

def get_frames() -> Dict[str, Any]:
    """Get all the available frames in the current Figma document.

    Returns:
        Dict containing the frames information or error message
    """
    try:
        result = send_command_to_figma("get_frames")
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(result, indent=2)
                }
            ]
        }
    except Exception as error:
        error_msg = str(error)
        logger.error(f"Error getting frames: {error_msg}")
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Error getting frames: {error_msg}"
                }
            ]
        }

def get_frame(name: str) -> Dict[str, Any]:
    """Get a specific frame by name from the current Figma document.

    Args:
        name: The name of the frame to get

    Returns:
        Dict containing the frame information or error message
    """
    try:
        result = send_command_to_figma("get_frame", {"name": name})
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(result, indent=2)
                }
            ]
        }
    except Exception as error:
        error_msg = str(error)
        logger.error(f"Error getting frame: {error_msg}")
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Error getting frame: {error_msg}"
                }
            ]
        }

def get_selection() -> Dict[str, Any]:
    """Get information about the current selection in Figma.

    Returns:
        Dict containing the selection information or error message
    """
    try:
        result = send_command_to_figma("get_selection")
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(result, indent=2)
                }
            ]
        }
    except Exception as error:
        error_msg = str(error)
        logger.error(f"Error getting selection: {error_msg}")
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Error getting selection: {error_msg}"
                }
            ]
        }

def read_my_design() -> Dict[str, Any]:
    """Get detailed information about the current selection in Figma, including all node details.

    Returns:
        Dict containing the design information or error message
    """
    try:
        result = send_command_to_figma("read_my_design")
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(result, indent=2)
                }
            ]
        }
    except Exception as error:
        error_msg = str(error)
        logger.error(f"Error reading design: {error_msg}")
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Error reading design: {error_msg}"
                }
            ]
        }

def get_node_info(node_id: str) -> Dict[str, Any]:
    """Get detailed information about a specific node in Figma.

    Args:
        node_id: The ID of the node to get information about

    Returns:
        Dict containing the node information or error message
    """
    try:
        result = send_command_to_figma("get_node_info", {"nodeId": node_id})
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(result, indent=2)
                }
            ]
        }
    except Exception as error:
        error_msg = str(error)
        logger.error(f"Error getting node info: {error_msg}")
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Error getting node info: {error_msg}"
                }
            ]
        }

def get_nodes_info(node_ids: list[str]) -> Dict[str, Any]:
    """Get detailed information about multiple nodes in Figma.

    Args:
        node_ids: Array of node IDs to get information about

    Returns:
        Dict containing the nodes information or error message
    """
    try:
        result = send_command_to_figma("get_nodes_info", {"nodeIds": node_ids})
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(result, indent=2)
                }
            ]
        }
    except Exception as error:
        error_msg = str(error)
        logger.error(f"Error getting nodes info: {error_msg}")
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Error getting nodes info: {error_msg}"
                }
            ]
        }

def create_rectangle(x: float, y: float, width: float, height: float, name: Optional[str] = None, parent_id: Optional[str] = None) -> Dict[str, Any]:
    """Create a new rectangle in Figma.

    Args:
        x: X position
        y: Y position
        width: Width of the rectangle
        height: Height of the rectangle
        name: Optional name for the rectangle
        parent_id: Optional parent node ID to append the rectangle to

    Returns:
        Dict containing the creation result or error message
    """
    try:
        result = send_command_to_figma("create_rectangle", {
            "x": x,
            "y": y,
            "width": width,
            "height": height,
            "name": name or "Rectangle",
            "parentId": parent_id
        })
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Created rectangle: {json.dumps(result, indent=2)}"
                }
            ]
        }
    except Exception as error:
        error_msg = str(error)
        logger.error(f"Error creating rectangle: {error_msg}")
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Error creating rectangle: {error_msg}"
                }
            ]
        }

def create_frame(
    x: float,
    y: float,
    width: float,
    height: float,
    name: Optional[str] = None,
    parent_id: Optional[str] = None,
    fill_color: Optional[Dict[str, float]] = None,
    stroke_color: Optional[Dict[str, float]] = None,
    stroke_weight: Optional[float] = None,
    layout_mode: Optional[str] = None,
    layout_wrap: Optional[str] = None,
    padding_top: Optional[float] = None,
    padding_right: Optional[float] = None,
    padding_bottom: Optional[float] = None,
    padding_left: Optional[float] = None,
    primary_axis_align_items: Optional[str] = None,
    counter_axis_align_items: Optional[str] = None,
    layout_sizing_horizontal: Optional[str] = None,
    layout_sizing_vertical: Optional[str] = None,
    item_spacing: Optional[float] = None
) -> Dict[str, Any]:
    """Create a new frame in Figma.

    Args:
        x: X position
        y: Y position
        width: Width of the frame
        height: Height of the frame
        name: Optional name for the frame
        parent_id: Optional parent node ID to append the frame to
        fill_color: Optional fill color in RGBA format
        stroke_color: Optional stroke color in RGBA format
        stroke_weight: Optional stroke weight
        layout_mode: Optional auto-layout mode
        layout_wrap: Optional wrap behavior
        padding_top: Optional top padding
        padding_right: Optional right padding
        padding_bottom: Optional bottom padding
        padding_left: Optional left padding
        primary_axis_align_items: Optional primary axis alignment
        counter_axis_align_items: Optional counter axis alignment
        layout_sizing_horizontal: Optional horizontal sizing mode
        layout_sizing_vertical: Optional vertical sizing mode
        item_spacing: Optional distance between children

    Returns:
        Dict containing the creation result or error message
    """
    try:
        result = send_command_to_figma("create_frame", {
            "x": x,
            "y": y,
            "width": width,
            "height": height,
            "name": name or "Frame",
            "parentId": parent_id,
            "fillColor": fill_color or {"r": 1, "g": 1, "b": 1, "a": 1},
            "strokeColor": stroke_color,
            "strokeWeight": stroke_weight,
            "layoutMode": layout_mode,
            "layoutWrap": layout_wrap,
            "paddingTop": padding_top,
            "paddingRight": padding_right,
            "paddingBottom": padding_bottom,
            "paddingLeft": padding_left,
            "primaryAxisAlignItems": primary_axis_align_items,
            "counterAxisAlignItems": counter_axis_align_items,
            "layoutSizingHorizontal": layout_sizing_horizontal,
            "layoutSizingVertical": layout_sizing_vertical,
            "itemSpacing": item_spacing
        })
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Created frame: {json.dumps(result, indent=2)}"
                }
            ]
        }
    except Exception as error:
        error_msg = str(error)
        logger.error(f"Error creating frame: {error_msg}")
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Error creating frame: {error_msg}"
                }
            ]
        }

def create_text(
    x: float,
    y: float,
    text: str,
    font_size: Optional[float] = None,
    font_weight: Optional[int] = None,
    font_color: Optional[Dict[str, float]] = None,
    name: Optional[str] = None,
    parent_id: Optional[str] = None
) -> Dict[str, Any]:
    """Create a new text element in Figma.

    Args:
        x: X position
        y: Y position
        text: Text content
        font_size: Optional font size
        font_weight: Optional font weight
        font_color: Optional font color in RGBA format
        name: Optional name for the text element
        parent_id: Optional parent node ID to append the text to

    Returns:
        Dict containing the creation result or error message
    """
    try:
        result = send_command_to_figma("create_text", {
            "x": x,
            "y": y,
            "text": text,
            "fontSize": font_size or 14,
            "fontWeight": font_weight or 400,
            "fontColor": font_color or {"r": 0, "g": 0, "b": 0, "a": 1},
            "name": name or "Text",
            "parentId": parent_id
        })
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Created text: {json.dumps(result, indent=2)}"
                }
            ]
        }
    except Exception as error:
        error_msg = str(error)
        logger.error(f"Error creating text: {error_msg}")
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Error creating text: {error_msg}"
                }
            ]
        }

def set_fill_color(node_id: str, r: float, g: float, b: float, a: float = 1.0) -> Dict[str, Any]:
    """Set the fill color of a node in Figma.

    Args:
        node_id: The ID of the node to modify
        r: Red component (0-1)
        g: Green component (0-1)
        b: Blue component (0-1)
        a: Optional alpha component (0-1)

    Returns:
        Dict containing the result or error message
    """
    try:
        result = send_command_to_figma("set_fill_color", {
            "nodeId": node_id,
            "color": {"r": r, "g": g, "b": b, "a": a}
        })
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Set fill color: {json.dumps(result, indent=2)}"
                }
            ]
        }
    except Exception as error:
        error_msg = str(error)
        logger.error(f"Error setting fill color: {error_msg}")
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Error setting fill color: {error_msg}"
                }
            ]
        }

def set_stroke_color(node_id: str, r: float, g: float, b: float, a: float = 1.0, weight: Optional[float] = None) -> Dict[str, Any]:
    """Set the stroke color of a node in Figma.

    Args:
        node_id: The ID of the node to modify
        r: Red component (0-1)
        g: Green component (0-1)
        b: Blue component (0-1)
        a: Optional alpha component (0-1)
        weight: Optional stroke weight

    Returns:
        Dict containing the result or error message
    """
    try:
        result = send_command_to_figma("set_stroke_color", {
            "nodeId": node_id,
            "color": {"r": r, "g": g, "b": b, "a": a},
            "weight": weight
        })
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Set stroke color: {json.dumps(result, indent=2)}"
                }
            ]
        }
    except Exception as error:
        error_msg = str(error)
        logger.error(f"Error setting stroke color: {error_msg}")
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Error setting stroke color: {error_msg}"
                }
            ]
        }

def move_node(node_id: str, x: float, y: float) -> Dict[str, Any]:
    """Move a node to a new position in Figma.

    Args:
        node_id: The ID of the node to move
        x: New X position
        y: New Y position

    Returns:
        Dict containing the result or error message
    """
    try:
        result = send_command_to_figma("move_node", {
            "nodeId": node_id,
            "x": x,
            "y": y
        })
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Moved node: {json.dumps(result, indent=2)}"
                }
            ]
        }
    except Exception as error:
        error_msg = str(error)
        logger.error(f"Error moving node: {error_msg}")
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Error moving node: {error_msg}"
                }
            ]
        }

def clone_node(node_id: str, x: Optional[float] = None, y: Optional[float] = None) -> Dict[str, Any]:
    """Clone an existing node in Figma.

    Args:
        node_id: The ID of the node to clone
        x: Optional new X position for the clone
        y: Optional new Y position for the clone

    Returns:
        Dict containing the result or error message
    """
    try:
        result = send_command_to_figma("clone_node", {
            "nodeId": node_id,
            "x": x,
            "y": y
        })
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Cloned node: {json.dumps(result, indent=2)}"
                }
            ]
        }
    except Exception as error:
        error_msg = str(error)
        logger.error(f"Error cloning node: {error_msg}")
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Error cloning node: {error_msg}"
                }
            ]
        }

def resize_node(node_id: str, width: float, height: float) -> Dict[str, Any]:
    """Resize a node in Figma.

    Args:
        node_id: The ID of the node to resize
        width: New width
        height: New height

    Returns:
        Dict containing the result or error message
    """
    try:
        result = send_command_to_figma("resize_node", {
            "nodeId": node_id,
            "width": width,
            "height": height
        })
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Resized node: {json.dumps(result, indent=2)}"
                }
            ]
        }
    except Exception as error:
        error_msg = str(error)
        logger.error(f"Error resizing node: {error_msg}")
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Error resizing node: {error_msg}"
                }
            ]
        }

def delete_node(node_id: str) -> Dict[str, Any]:
    """Delete a node from Figma.

    Args:
        node_id: The ID of the node to delete

    Returns:
        Dict containing the result or error message
    """
    try:
        result = send_command_to_figma("delete_node", {
            "nodeId": node_id
        })
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Deleted node: {json.dumps(result, indent=2)}"
                }
            ]
        }
    except Exception as error:
        error_msg = str(error)
        logger.error(f"Error deleting node: {error_msg}")
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Error deleting node: {error_msg}"
                }
            ]
        }

def delete_multiple_nodes(node_ids: list[str]) -> Dict[str, Any]:
    """Delete multiple nodes from Figma at once.

    Args:
        node_ids: Array of node IDs to delete

    Returns:
        Dict containing the result or error message
    """
    try:
        result = send_command_to_figma("delete_multiple_nodes", {
            "nodeIds": node_ids
        })
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Deleted nodes: {json.dumps(result, indent=2)}"
                }
            ]
        }
    except Exception as error:
        error_msg = str(error)
        logger.error(f"Error deleting multiple nodes: {error_msg}")
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Error deleting multiple nodes: {error_msg}"
                }
            ]
        }

def export_node_as_image(node_id: str, format: str = "PNG", scale: float = 1.0) -> Dict[str, Any]:
    """Export a node as an image from Figma.

    Args:
        node_id: The ID of the node to export
        format: Optional export format (PNG, JPG, SVG, PDF)
        scale: Optional export scale

    Returns:
        Dict containing the result or error message
    """
    try:
        result = send_command_to_figma("export_node_as_image", {
            "nodeId": node_id,
            "format": format,
            "scale": scale
        })
        return {
            "content": [
                {
                    "type": "image",
                    "data": result["imageData"],
                    "mimeType": result["mimeType"] or "image/png"
                }
            ]
        }
    except Exception as error:
        error_msg = str(error)
        logger.error(f"Error exporting node as image: {error_msg}")
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Error exporting node as image: {error_msg}"
                }
            ]
        }

def set_text_content(node_id: str, text: str) -> Dict[str, Any]:
    """Set the text content of an existing text node in Figma.

    Args:
        node_id: The ID of the text node to modify
        text: New text content

    Returns:
        Dict containing the result or error message
    """
    try:
        result = send_command_to_figma("set_text_content", {
            "nodeId": node_id,
            "text": text
        })
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Updated text content: {json.dumps(result, indent=2)}"
                }
            ]
        }
    except Exception as error:
        error_msg = str(error)
        logger.error(f"Error setting text content: {error_msg}")
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Error setting text content: {error_msg}"
                }
            ]
        }

def get_styles() -> Dict[str, Any]:
    """Get all styles from the current Figma document.

    Returns:
        Dict containing the styles information or error message
    """
    try:
        result = send_command_to_figma("get_styles")
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(result, indent=2)
                }
            ]
        }
    except Exception as error:
        error_msg = str(error)
        logger.error(f"Error getting styles: {error_msg}")
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Error getting styles: {error_msg}"
                }
            ]
        }

def get_local_components() -> Dict[str, Any]:
    """Get all local components from the Figma document.

    Returns:
        Dict containing the components information or error message
    """
    try:
        result = send_command_to_figma("get_local_components")
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(result, indent=2)
                }
            ]
        }
    except Exception as error:
        error_msg = str(error)
        logger.error(f"Error getting local components: {error_msg}")
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Error getting local components: {error_msg}"
                }
            ]
        }

def create_component_instance(component_key: str, x: float, y: float) -> Dict[str, Any]:
    """Create an instance of a component in Figma.

    Args:
        component_key: Key of the component to instantiate
        x: X position
        y: Y position

    Returns:
        Dict containing the result or error message
    """
    try:
        result = send_command_to_figma("create_component_instance", {
            "componentKey": component_key,
            "x": x,
            "y": y
        })
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Created component instance: {json.dumps(result, indent=2)}"
                }
            ]
        }
    except Exception as error:
        error_msg = str(error)
        logger.error(f"Error creating component instance: {error_msg}")
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Error creating component instance: {error_msg}"
                }
            ]
        }

def get_instance_overrides(node_id: Optional[str] = None) -> Dict[str, Any]:
    """Get all override properties from a selected component instance.

    Args:
        node_id: Optional ID of the component instance to get overrides from

    Returns:
        Dict containing the overrides information or error message
    """
    try:
        result = send_command_to_figma("get_instance_overrides", {
            "instanceNodeId": node_id
        })
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(result, indent=2)
                }
            ]
        }
    except Exception as error:
        error_msg = str(error)
        logger.error(f"Error getting instance overrides: {error_msg}")
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Error getting instance overrides: {error_msg}"
                }
            ]
        }

def set_instance_overrides(source_instance_id: str, target_node_ids: list[str]) -> Dict[str, Any]:
    """Apply previously copied overrides to selected component instances.

    Args:
        source_instance_id: ID of the source component instance
        target_node_ids: Array of target instance IDs

    Returns:
        Dict containing the result or error message
    """
    try:
        result = send_command_to_figma("set_instance_overrides", {
            "sourceInstanceId": source_instance_id,
            "targetNodeIds": target_node_ids
        })
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Applied instance overrides: {json.dumps(result, indent=2)}"
                }
            ]
        }
    except Exception as error:
        error_msg = str(error)
        logger.error(f"Error setting instance overrides: {error_msg}")
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Error setting instance overrides: {error_msg}"
                }
            ]
        }

def set_corner_radius(node_id: str, radius: float, corners: Optional[list[bool]] = None) -> Dict[str, Any]:
    """Set the corner radius of a node in Figma.

    Args:
        node_id: The ID of the node to modify
        radius: Corner radius value
        corners: Optional array of 4 booleans to specify which corners to round

    Returns:
        Dict containing the result or error message
    """
    try:
        result = send_command_to_figma("set_corner_radius", {
            "nodeId": node_id,
            "radius": radius,
            "corners": corners or [True, True, True, True]
        })
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Set corner radius: {json.dumps(result, indent=2)}"
                }
            ]
        }
    except Exception as error:
        error_msg = str(error)
        logger.error(f"Error setting corner radius: {error_msg}")
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Error setting corner radius: {error_msg}"
                }
            ]
        }

def scan_text_nodes(node_id: str) -> Dict[str, Any]:
    """Scan all text nodes in the selected Figma node.

    Args:
        node_id: ID of the node to scan

    Returns:
        Dict containing the scan results or error message
    """
    try:
        result = send_command_to_figma("scan_text_nodes", {
            "nodeId": node_id,
            "useChunking": True,
            "chunkSize": 10
        })
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(result, indent=2)
                }
            ]
        }
    except Exception as error:
        error_msg = str(error)
        logger.error(f"Error scanning text nodes: {error_msg}")
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Error scanning text nodes: {error_msg}"
                }
            ]
        }

def set_multiple_text_contents(node_id: str, text: list[Dict[str, str]]) -> Dict[str, Any]:
    """Set multiple text contents parallelly in a node.

    Args:
        node_id: The ID of the node containing the text nodes to replace
        text: Array of text node IDs and their replacement texts

    Returns:
        Dict containing the result or error message
    """
    try:
        result = send_command_to_figma("set_multiple_text_contents", {
            "nodeId": node_id,
            "text": text
        })
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Updated multiple text contents: {json.dumps(result, indent=2)}"
                }
            ]
        }
    except Exception as error:
        error_msg = str(error)
        logger.error(f"Error setting multiple text contents: {error_msg}")
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Error setting multiple text contents: {error_msg}"
                }
            ]
        }

def get_annotations(node_id: Optional[str] = None, include_categories: bool = True) -> Dict[str, Any]:
    """Get all annotations in the current document or specific node.

    Args:
        node_id: Optional node ID to get annotations for specific node
        include_categories: Whether to include category information

    Returns:
        Dict containing the annotations information or error message
    """
    try:
        result = send_command_to_figma("get_annotations", {
            "nodeId": node_id,
            "includeCategories": include_categories
        })
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(result, indent=2)
                }
            ]
        }
    except Exception as error:
        error_msg = str(error)
        logger.error(f"Error getting annotations: {error_msg}")
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Error getting annotations: {error_msg}"
                }
            ]
        }

def set_annotation(
    node_id: str,
    label_markdown: str,
    annotation_id: Optional[str] = None,
    category_id: Optional[str] = None,
    properties: Optional[list[Dict[str, str]]] = None
) -> Dict[str, Any]:
    """Create or update an annotation.

    Args:
        node_id: The ID of the node to annotate
        label_markdown: The annotation text in markdown format
        annotation_id: Optional ID of the annotation to update
        category_id: Optional ID of the annotation category
        properties: Optional additional properties for the annotation

    Returns:
        Dict containing the result or error message
    """
    try:
        result = send_command_to_figma("set_annotation", {
            "nodeId": node_id,
            "annotationId": annotation_id,
            "labelMarkdown": label_markdown,
            "categoryId": category_id,
            "properties": properties
        })
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Set annotation: {json.dumps(result, indent=2)}"
                }
            ]
        }
    except Exception as error:
        error_msg = str(error)
        logger.error(f"Error setting annotation: {error_msg}")
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Error setting annotation: {error_msg}"
                }
            ]
        }

def set_multiple_annotations(node_id: str, annotations: list[Dict[str, Any]]) -> Dict[str, Any]:
    """Set multiple annotations parallelly in a node.

    Args:
        node_id: The ID of the node containing the elements to annotate
        annotations: Array of annotations to apply

    Returns:
        Dict containing the result or error message
    """
    try:
        result = send_command_to_figma("set_multiple_annotations", {
            "nodeId": node_id,
            "annotations": annotations
        })
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Set multiple annotations: {json.dumps(result, indent=2)}"
                }
            ]
        }
    except Exception as error:
        error_msg = str(error)
        logger.error(f"Error setting multiple annotations: {error_msg}")
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Error setting multiple annotations: {error_msg}"
                }
            ]
        }

def scan_nodes_by_types(node_id: str, types: list[str]) -> Dict[str, Any]:
    """Scan for child nodes with specific types in the selected Figma node.

    Args:
        node_id: ID of the node to scan
        types: Array of node types to find in the child nodes

    Returns:
        Dict containing the scan results or error message
    """
    try:
        result = send_command_to_figma("scan_nodes_by_types", {
            "nodeId": node_id,
            "types": types
        })
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(result, indent=2)
                }
            ]
        }
    except Exception as error:
        error_msg = str(error)
        logger.error(f"Error scanning nodes by types: {error_msg}")
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Error scanning nodes by types: {error_msg}"
                }
            ]
        }

def set_layout_mode(node_id: str, layout_mode: str, layout_wrap: Optional[str] = None) -> Dict[str, Any]:
    """Set the layout mode and wrap behavior of a frame in Figma.

    Args:
        node_id: The ID of the frame to modify
        layout_mode: Layout mode for the frame
        layout_wrap: Optional wrap behavior

    Returns:
        Dict containing the result or error message
    """
    try:
        result = send_command_to_figma("set_layout_mode", {
            "nodeId": node_id,
            "layoutMode": layout_mode,
            "layoutWrap": layout_wrap or "NO_WRAP"
        })
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Set layout mode: {json.dumps(result, indent=2)}"
                }
            ]
        }
    except Exception as error:
        error_msg = str(error)
        logger.error(f"Error setting layout mode: {error_msg}")
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Error setting layout mode: {error_msg}"
                }
            ]
        }

def set_padding(
    node_id: str,
    padding_top: Optional[float] = None,
    padding_right: Optional[float] = None,
    padding_bottom: Optional[float] = None,
    padding_left: Optional[float] = None
) -> Dict[str, Any]:
    """Set padding values for an auto-layout frame in Figma.

    Args:
        node_id: The ID of the frame to modify
        padding_top: Optional top padding value
        padding_right: Optional right padding value
        padding_bottom: Optional bottom padding value
        padding_left: Optional left padding value

    Returns:
        Dict containing the result or error message
    """
    try:
        result = send_command_to_figma("set_padding", {
            "nodeId": node_id,
            "paddingTop": padding_top,
            "paddingRight": padding_right,
            "paddingBottom": padding_bottom,
            "paddingLeft": padding_left
        })
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Set padding: {json.dumps(result, indent=2)}"
                }
            ]
        }
    except Exception as error:
        error_msg = str(error)
        logger.error(f"Error setting padding: {error_msg}")
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Error setting padding: {error_msg}"
                }
            ]
        }

def set_axis_align(
    node_id: str,
    primary_axis_align_items: Optional[str] = None,
    counter_axis_align_items: Optional[str] = None
) -> Dict[str, Any]:
    """Set primary and counter axis alignment for an auto-layout frame in Figma.

    Args:
        node_id: The ID of the frame to modify
        primary_axis_align_items: Optional primary axis alignment
        counter_axis_align_items: Optional counter axis alignment

    Returns:
        Dict containing the result or error message
    """
    try:
        result = send_command_to_figma("set_axis_align", {
            "nodeId": node_id,
            "primaryAxisAlignItems": primary_axis_align_items,
            "counterAxisAlignItems": counter_axis_align_items
        })
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Set axis alignment: {json.dumps(result, indent=2)}"
                }
            ]
        }
    except Exception as error:
        error_msg = str(error)
        logger.error(f"Error setting axis alignment: {error_msg}")
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Error setting axis alignment: {error_msg}"
                }
            ]
        }

def set_layout_sizing(
    node_id: str,
    layout_sizing_horizontal: Optional[str] = None,
    layout_sizing_vertical: Optional[str] = None
) -> Dict[str, Any]:
    """Set horizontal and vertical sizing modes for an auto-layout frame in Figma.

    Args:
        node_id: The ID of the frame to modify
        layout_sizing_horizontal: Optional horizontal sizing mode
        layout_sizing_vertical: Optional vertical sizing mode

    Returns:
        Dict containing the result or error message
    """
    try:
        result = send_command_to_figma("set_layout_sizing", {
            "nodeId": node_id,
            "layoutSizingHorizontal": layout_sizing_horizontal,
            "layoutSizingVertical": layout_sizing_vertical
        })
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Set layout sizing: {json.dumps(result, indent=2)}"
                }
            ]
        }
    except Exception as error:
        error_msg = str(error)
        logger.error(f"Error setting layout sizing: {error_msg}")
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Error setting layout sizing: {error_msg}"
                }
            ]
        }

def set_item_spacing(node_id: str, item_spacing: float) -> Dict[str, Any]:
    """Set distance between children in an auto-layout frame.

    Args:
        node_id: The ID of the frame to modify
        item_spacing: Distance between children

    Returns:
        Dict containing the result or error message
    """
    try:
        result = send_command_to_figma("set_item_spacing", {
            "nodeId": node_id,
            "itemSpacing": item_spacing
        })
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Set item spacing: {json.dumps(result, indent=2)}"
                }
            ]
        }
    except Exception as error:
        error_msg = str(error)
        logger.error(f"Error setting item spacing: {error_msg}")
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Error setting item spacing: {error_msg}"
                }
            ]
        }

def get_reactions(node_ids: list[str]) -> Dict[str, Any]:
    """Get Figma Prototyping Reactions from multiple nodes.

    Args:
        node_ids: Array of node IDs to get reactions from

    Returns:
        Dict containing the reactions information or error message
    """
    try:
        result = send_command_to_figma("get_reactions", {
            "nodeIds": node_ids
        })
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(result, indent=2)
                },
                {
                    "type": "text",
                    "text": "IMPORTANT: You MUST now use the reaction data above and follow the `reaction_to_connector_strategy` prompt to prepare the parameters for the `create_connections` tool call. This is a required next step."
                }
            ],
            "followUp": {
                "type": "prompt",
                "prompt": "reaction_to_connector_strategy"
            }
        }
    except Exception as error:
        error_msg = str(error)
        logger.error(f"Error getting reactions: {error_msg}")
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Error getting reactions: {error_msg}"
                }
            ]
        }

def set_default_connector(connector_id: Optional[str] = None) -> Dict[str, Any]:
    """Set a copied connector node as the default connector.

    Args:
        connector_id: Optional ID of the connector node to set as default

    Returns:
        Dict containing the result or error message
    """
    try:
        result = send_command_to_figma("set_default_connector", {
            "connectorId": connector_id
        })
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Default connector set: {json.dumps(result, indent=2)}"
                }
            ]
        }
    except Exception as error:
        error_msg = str(error)
        logger.error(f"Error setting default connector: {error_msg}")
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Error setting default connector: {error_msg}"
                }
            ]
        }

def create_connections(connections: list[Dict[str, str]]) -> Dict[str, Any]:
    """Create connections between nodes using the default connector style.

    Args:
        connections: Array of node connections to create

    Returns:
        Dict containing the result or error message
    """
    try:
        result = send_command_to_figma("create_connections", {
            "connections": connections
        })
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Created connections: {json.dumps(result, indent=2)}"
                }
            ]
        }
    except Exception as error:
        error_msg = str(error)
        logger.error(f"Error creating connections: {error_msg}")
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Error creating connections: {error_msg}"
                }
            ]
        }

connect_to_figma()
root_agent = LlmAgent(
    model='gemini-2.5-flash-preview-05-20',
    name='figma_agent',
    instruction='You are an autonomous UX designer. You can perform CRUD operations on figma components. You can convert the user requirements into beautiful and standardized figma updations.',
    tools=[
        join_channel,
        get_document_info,
        get_frames,
        get_frame,
        get_selection,
        read_my_design,
        get_node_info,
        get_nodes_info,
        create_rectangle,
        create_frame,
        create_text,
        set_fill_color,
        set_stroke_color,
        move_node,
        clone_node,
        resize_node,
        delete_node,
        delete_multiple_nodes,
        export_node_as_image,
        set_text_content,
        get_styles,
        get_local_components,
        create_component_instance,
        get_instance_overrides,
        set_instance_overrides,
        set_corner_radius,
        scan_text_nodes,
        set_multiple_text_contents,
        get_annotations,
        set_annotation,
        set_multiple_annotations,
        scan_nodes_by_types,
        set_layout_mode,
        set_padding,
        set_axis_align,
        set_layout_sizing,
        set_item_spacing,
        get_reactions,
        set_default_connector,
        create_connections
    ]
)
