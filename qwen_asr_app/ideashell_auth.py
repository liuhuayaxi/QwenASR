"""MCP OAuth adapter that restores expiration and discovery across app restarts.

Imported lazily by IdeaShellConnection. The SDK still owns PKCE, registration,
issuer validation, token exchange, and token refresh.
"""
from __future__ import annotations

import json
import time

import httpx2
from mcp.client.auth import OAuthClientProvider
from mcp.client.auth.utils import (
    build_oauth_authorization_server_metadata_discovery_urls,
    build_protected_resource_metadata_discovery_urls,
    create_oauth_metadata_request,
    credentials_match_issuer,
    handle_auth_metadata_response,
    handle_protected_resource_response,
    validate_metadata_issuer,
)
from mcp.shared.auth import OAuthMetadata, ProtectedResourceMetadata


class PersistentOAuthProvider(OAuthClientProvider):
    async def _initialize(self):
        await super()._initialize()
        token = self.context.current_tokens
        # MCP 2 reloads the token but does not restore token_expiry_time.
        # Without this, an expired token goes straight to a 401 and browser auth.
        if token is not None and token.expires_in is not None:
            margin = 15 if self.context.can_refresh_token() else 0
            self.context.token_expiry_time = time.time() + max(0, token.expires_in) - margin
        saved = self.context.storage.get_discovery()
        if saved.get("server_url") != self.context.server_url:
            return
        try:
            metadata = OAuthMetadata.model_validate(saved["oauth_metadata"])
            resource = ProtectedResourceMetadata.model_validate(saved["protected_resource_metadata"]) if saved.get("protected_resource_metadata") else None
            issuer = saved.get("auth_server_url") or str(metadata.issuer)
            validate_metadata_issuer(metadata, issuer)
            if self.context.client_info and not credentials_match_issuer(self.context.client_info, issuer, self.context.client_metadata_url):
                return
            if resource:
                await self._validate_resource_match(resource)
            self.context.oauth_metadata = metadata
            self.context.protected_resource_metadata = resource
            self.context.auth_server_url = issuer
        except (ValueError, KeyError):
            # Missing/obsolete metadata is rediscovered; valid tokens remain saved.
            return

    def _save_discovery(self):
        context = self.context
        if context.oauth_metadata is None:
            return
        self.context.storage.set_discovery({
            "server_url": context.server_url,
            "auth_server_url": context.auth_server_url,
            "oauth_metadata": context.oauth_metadata.model_dump(mode="json", exclude_none=True),
            "protected_resource_metadata": context.protected_resource_metadata.model_dump(mode="json", exclude_none=True) if context.protected_resource_metadata else None,
        })

    async def _discover_refresh_metadata(self):
        # Older credential files lack a discovered token endpoint. Find it before
        # refreshing, rather than guessing /token on the MCP resource host.
        async with httpx2.AsyncClient(follow_redirects=True, timeout=25) as client:
            for url in build_protected_resource_metadata_discovery_urls(None, self.context.server_url):
                response = await client.send(create_oauth_metadata_request(url))
                if response.status_code not in {200, 404}:
                    raise RuntimeError(f"暂时无法读取闪念贝壳授权配置（HTTP {response.status_code}），本机凭证已保留")
                resource = await handle_protected_resource_response(response)
                if resource:
                    await self._validate_resource_match(resource)
                    self.context.protected_resource_metadata = resource
                    self.context.auth_server_url = str(resource.authorization_servers[0])
                    break
            issuer = self.context.auth_server_url
            if not issuer and self.context.client_info:
                issuer = self.context.client_info.issuer
            for url in build_oauth_authorization_server_metadata_discovery_urls(issuer, self.context.server_url):
                response = await client.send(create_oauth_metadata_request(url))
                if response.status_code not in {200, 404}:
                    raise RuntimeError(f"暂时无法读取闪念贝壳刷新端点（HTTP {response.status_code}），本机凭证已保留")
                _, metadata = await handle_auth_metadata_response(response)
                if metadata:
                    if issuer:
                        validate_metadata_issuer(metadata, issuer)
                    actual_issuer = issuer or str(metadata.issuer)
                    if self.context.client_info and not credentials_match_issuer(self.context.client_info, actual_issuer, self.context.client_metadata_url):
                        raise RuntimeError("闪念贝壳授权服务器已变化，请重新连接授权")
                    self.context.oauth_metadata = metadata
                    self.context.auth_server_url = actual_issuer
                    self._save_discovery()
                    return
        raise RuntimeError("未能读取闪念贝壳刷新端点，本机凭证已保留，请稍后重试连接")

    async def _refresh_token(self):
        if self.context.oauth_metadata is None:
            await self._discover_refresh_metadata()
        return await super()._refresh_token()

    async def _handle_refresh_response(self, response):
        if response.status_code != 200:
            try:
                payload = json.loads(await response.aread())
                error = payload.get("error", "") if isinstance(payload, dict) else ""
            except (ValueError, UnicodeError):
                error = ""
            # Transient failures must not clear a long-lived refresh token or
            # fall through to interactive authorization.
            rejected = error in {"invalid_grant", "invalid_token", "invalid_client"} or response.status_code in {401, 403}
            if not rejected:
                raise RuntimeError(f"闪念贝壳授权刷新暂未完成（HTTP {response.status_code}），本机凭证已保留，请稍后重试")
            if error == "invalid_client":
                self.context.client_info = None
        result = await super()._handle_refresh_response(response)
        if response.status_code == 200 and not result:
            raise RuntimeError("闪念贝壳刷新响应无法读取，本机凭证已保留，请稍后重试")
        if result:
            self._save_discovery()
        return result

    async def _handle_token_response(self, response):
        await super()._handle_token_response(response)
        self._save_discovery()

    async def async_auth_flow(self, request):
        # A token can be rejected before its stated expiry. Refresh once on a
        # definite 401 before allowing the SDK to start interactive OAuth.
        flow = super().async_auth_flow(request)
        refresh_attempted = False
        try:
            outgoing = await flow.__anext__()
            while True:
                if (outgoing is not request and self.context.oauth_metadata
                        and str(outgoing.url) == str(self.context.oauth_metadata.token_endpoint)):
                    refresh_attempted = True
                response = yield outgoing
                if (outgoing is request and response.status_code == 401
                        and self.context.can_refresh_token() and not refresh_attempted):
                    refresh_attempted = True
                    refresh = await self._refresh_token()
                    refreshed = yield refresh
                    if await self._handle_refresh_response(refreshed):
                        self._add_auth_header(request)
                        response = yield request
                outgoing = await flow.asend(response)
        except StopAsyncIteration:
            return
        finally:
            await flow.aclose()
