"""Shared mutable state for the desktop process."""

# Set to True once the local Flask server is ready to accept connections
server_ready: bool = False

# Port the local server is running on
server_port: int = 5000
