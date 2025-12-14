"""
Hetzner Cloud API Service

Provides async interface for managing Hetzner Cloud resources:
- Servers (create, list, delete, actions)
- SSH Keys (create, list, delete)

Uses httpx for async HTTP requests.
"""

import os
import httpx
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime

from config.logging_config import get_logger
from models import (
    HetznerServerInfo,
    HetznerServerStatus,
    HetznerSSHKeyInfo,
)

logger = get_logger(__name__)


class HetznerAPIError(Exception):
    """Custom exception for Hetzner API errors."""
    def __init__(self, message: str, status_code: int = None, error_code: str = None):
        self.message = message
        self.status_code = status_code
        self.error_code = error_code
        super().__init__(self.message)


class HetznerService:
    """
    Async service for interacting with the Hetzner Cloud API.

    Usage:
        service = HetznerService()
        servers = await service.list_servers()
    """

    BASE_URL = "https://api.hetzner.cloud/v1"

    def __init__(self, api_token: Optional[str] = None):
        """
        Initialize Hetzner service.

        Args:
            api_token: Hetzner API token. If not provided, reads from:
                       1. HETZNER_API_TOKEN env var
                       2. secrets/hetzner_token.txt file
        """
        self.api_token = api_token or self._load_api_token()

        if not self.api_token:
            raise HetznerAPIError(
                "Hetzner API token not configured. "
                "Set HETZNER_API_TOKEN env var or create secrets/hetzner_token.txt"
            )

        self._client: Optional[httpx.AsyncClient] = None

    def _load_api_token(self) -> Optional[str]:
        """Load API token from environment or secrets file."""
        # Try environment variable first
        token = os.getenv("HETZNER_API_TOKEN")
        if token:
            logger.debug("Loaded Hetzner API token from environment")
            return token.strip()

        # Try secrets file
        secrets_paths = [
            Path("secrets/hetzner_token.txt"),
            Path("/app/secrets/hetzner_token.txt"),  # Docker path
        ]

        for secrets_path in secrets_paths:
            if secrets_path.exists():
                token = secrets_path.read_text().strip()
                if token:
                    logger.debug(f"Loaded Hetzner API token from {secrets_path}")
                    return token

        return None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create async HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.BASE_URL,
                headers={
                    "Authorization": f"Bearer {self.api_token}",
                    "Content-Type": "application/json",
                },
                timeout=30.0,
            )
        return self._client

    async def close(self):
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def _request(
        self,
        method: str,
        endpoint: str,
        json_data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Make an API request to Hetzner."""
        client = await self._get_client()

        try:
            response = await client.request(
                method=method,
                url=endpoint,
                json=json_data,
                params=params,
            )

            # Log request
            logger.debug(
                f"Hetzner API {method} {endpoint}",
                extra={"status_code": response.status_code}
            )

            # Handle errors
            if response.status_code >= 400:
                error_data = response.json() if response.content else {}
                error_info = error_data.get("error", {})
                raise HetznerAPIError(
                    message=error_info.get("message", f"HTTP {response.status_code}"),
                    status_code=response.status_code,
                    error_code=error_info.get("code"),
                )

            return response.json() if response.content else {}

        except httpx.RequestError as e:
            logger.error(f"Hetzner API request failed: {e}")
            raise HetznerAPIError(f"Request failed: {e}")

    # =========================================================================
    # Server Operations
    # =========================================================================

    async def list_servers(
        self,
        label_selector: Optional[str] = None,
        name: Optional[str] = None,
    ) -> List[HetznerServerInfo]:
        """
        List all servers.

        Args:
            label_selector: Filter by label (e.g., "env=prod")
            name: Filter by server name

        Returns:
            List of server info objects
        """
        params = {}
        if label_selector:
            params["label_selector"] = label_selector
        if name:
            params["name"] = name

        data = await self._request("GET", "/servers", params=params or None)

        servers = []
        for server_data in data.get("servers", []):
            servers.append(self._parse_server(server_data))

        logger.info(f"📋 Listed {len(servers)} Hetzner servers")
        return servers

    async def get_server(self, server_id: int) -> HetznerServerInfo:
        """Get details of a specific server."""
        data = await self._request("GET", f"/servers/{server_id}")
        return self._parse_server(data["server"])

    async def create_server(
        self,
        name: str,
        server_type: str = "cx21",
        image: str = "ubuntu-24.04",
        location: str = "nbg1",
        ssh_keys: Optional[List[str]] = None,
        user_data: Optional[str] = None,
        labels: Optional[Dict[str, str]] = None,
        start_after_create: bool = True,
    ) -> Dict[str, Any]:
        """
        Create a new server.

        Args:
            name: Server name (must be unique)
            server_type: Server type (cx21, cx31, etc.)
            image: OS image (ubuntu-24.04, debian-12, etc.)
            location: Datacenter (fsn1, nbg1, hel1, ash, hil)
            ssh_keys: List of SSH key names or IDs
            user_data: Cloud-init user data script
            labels: Labels for organization
            start_after_create: Start server after creation

        Returns:
            Dict with server info, root_password (if no SSH keys), and action_id
        """
        payload = {
            "name": name,
            "server_type": server_type,
            "image": image,
            "location": location,
            "start_after_create": start_after_create,
        }

        if ssh_keys:
            payload["ssh_keys"] = ssh_keys
        if user_data:
            payload["user_data"] = user_data
        if labels:
            payload["labels"] = labels

        logger.info(
            f"🚀 Creating Hetzner server: {name}",
            extra={
                "server_type": server_type,
                "image": image,
                "location": location,
            }
        )

        data = await self._request("POST", "/servers", json_data=payload)

        result = {
            "server": self._parse_server(data["server"]),
            "action_id": data.get("action", {}).get("id"),
        }

        # Root password is only returned if no SSH keys were provided
        if "root_password" in data:
            result["root_password"] = data["root_password"]

        logger.info(f"✅ Server created: {name} (ID: {result['server'].id})")
        return result

    async def delete_server(self, server_id: int) -> bool:
        """
        Delete a server.

        Args:
            server_id: Server ID to delete

        Returns:
            True if deletion was successful
        """
        logger.info(f"🗑️ Deleting Hetzner server: {server_id}")

        await self._request("DELETE", f"/servers/{server_id}")

        logger.info(f"✅ Server deleted: {server_id}")
        return True

    async def server_action(
        self,
        server_id: int,
        action: str,
        image: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Perform an action on a server.

        Args:
            server_id: Server ID
            action: Action to perform (poweron, poweroff, reboot, shutdown, rebuild)
            image: Image for rebuild action

        Returns:
            Action info
        """
        # Map action names
        action_map = {
            "start": "poweron",
            "stop": "poweroff",
            "reboot": "reboot",
            "shutdown": "shutdown",
            "rebuild": "rebuild",
        }
        api_action = action_map.get(action, action)

        payload = {}
        if api_action == "rebuild" and image:
            payload["image"] = image

        logger.info(f"⚡ Server action: {api_action} on {server_id}")

        data = await self._request(
            "POST",
            f"/servers/{server_id}/actions/{api_action}",
            json_data=payload if payload else None,
        )

        return {
            "action_id": data.get("action", {}).get("id"),
            "action_status": data.get("action", {}).get("status"),
        }

    # =========================================================================
    # SSH Key Operations
    # =========================================================================

    async def list_ssh_keys(
        self,
        name: Optional[str] = None,
        fingerprint: Optional[str] = None,
    ) -> List[HetznerSSHKeyInfo]:
        """List all SSH keys."""
        params = {}
        if name:
            params["name"] = name
        if fingerprint:
            params["fingerprint"] = fingerprint

        data = await self._request("GET", "/ssh_keys", params=params or None)

        keys = []
        for key_data in data.get("ssh_keys", []):
            keys.append(self._parse_ssh_key(key_data))

        logger.info(f"📋 Listed {len(keys)} SSH keys")
        return keys

    async def create_ssh_key(
        self,
        name: str,
        public_key: str,
        labels: Optional[Dict[str, str]] = None,
    ) -> HetznerSSHKeyInfo:
        """Create a new SSH key."""
        payload = {
            "name": name,
            "public_key": public_key,
        }
        if labels:
            payload["labels"] = labels

        logger.info(f"🔑 Creating SSH key: {name}")

        data = await self._request("POST", "/ssh_keys", json_data=payload)

        key = self._parse_ssh_key(data["ssh_key"])
        logger.info(f"✅ SSH key created: {name} (ID: {key.id})")
        return key

    async def delete_ssh_key(self, ssh_key_id: int) -> bool:
        """Delete an SSH key."""
        logger.info(f"🗑️ Deleting SSH key: {ssh_key_id}")

        await self._request("DELETE", f"/ssh_keys/{ssh_key_id}")

        logger.info(f"✅ SSH key deleted: {ssh_key_id}")
        return True

    # =========================================================================
    # Server Types & Images (read-only)
    # =========================================================================

    async def list_server_types(self) -> List[Dict[str, Any]]:
        """List available server types with pricing."""
        data = await self._request("GET", "/server_types")

        types = []
        for st in data.get("server_types", []):
            types.append({
                "name": st["name"],
                "description": st["description"],
                "cores": st["cores"],
                "memory": st["memory"],
                "disk": st["disk"],
                "prices": st.get("prices", []),
            })

        return types

    async def list_images(self, type_filter: str = "system") -> List[Dict[str, Any]]:
        """List available images."""
        params = {"type": type_filter}
        data = await self._request("GET", "/images", params=params)

        images = []
        for img in data.get("images", []):
            images.append({
                "id": img["id"],
                "name": img["name"],
                "description": img.get("description"),
                "type": img["type"],
                "os_flavor": img.get("os_flavor"),
                "os_version": img.get("os_version"),
            })

        return images

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _parse_server(self, data: Dict[str, Any]) -> HetznerServerInfo:
        """Parse server data from API response."""
        # Extract public IPs
        public_net = data.get("public_net", {})
        ipv4 = public_net.get("ipv4", {}).get("ip")
        ipv6 = public_net.get("ipv6", {}).get("ip")

        # Parse status
        status_str = data.get("status", "unknown")
        try:
            status = HetznerServerStatus(status_str)
        except ValueError:
            status = HetznerServerStatus.UNKNOWN

        # Get image name
        image = data.get("image")
        image_name = image.get("name") if isinstance(image, dict) else None

        # Get pricing
        server_type_data = data.get("server_type", {})
        prices = server_type_data.get("prices", [])
        monthly_cost = None
        hourly_cost = None
        if prices:
            # Get first price (usually the relevant datacenter)
            price = prices[0].get("price_monthly", {})
            monthly_cost = float(price.get("gross", 0)) if price else None
            hourly_price = prices[0].get("price_hourly", {})
            hourly_cost = float(hourly_price.get("gross", 0)) if hourly_price else None

        return HetznerServerInfo(
            id=data["id"],
            name=data["name"],
            status=status,
            public_ipv4=ipv4,
            public_ipv6=ipv6,
            server_type=server_type_data.get("name", "unknown"),
            datacenter=data.get("datacenter", {}).get("name", "unknown"),
            image=image_name,
            created=datetime.fromisoformat(data["created"].replace("Z", "+00:00")),
            labels=data.get("labels", {}),
            monthly_cost_eur=monthly_cost,
            hourly_cost_eur=hourly_cost,
        )

    def _parse_ssh_key(self, data: Dict[str, Any]) -> HetznerSSHKeyInfo:
        """Parse SSH key data from API response."""
        return HetznerSSHKeyInfo(
            id=data["id"],
            name=data["name"],
            fingerprint=data["fingerprint"],
            public_key=data["public_key"],
            labels=data.get("labels", {}),
            created=datetime.fromisoformat(data["created"].replace("Z", "+00:00")),
        )


# Global service instance (lazy initialization)
_hetzner_service: Optional[HetznerService] = None


def get_hetzner_service() -> HetznerService:
    """Get or create global Hetzner service instance."""
    global _hetzner_service
    if _hetzner_service is None:
        _hetzner_service = HetznerService()
    return _hetzner_service
