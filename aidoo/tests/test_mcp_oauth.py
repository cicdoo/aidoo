# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Aidoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""OAuth discovery / DCR / authorization-code + PKCE flow for /aidoo/mcp."""
import base64
import hashlib
import json
import re
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit

from odoo.tests.common import HttpCase, tagged

REDIRECT = "https://claude.ai/api/mcp/auth_callback"
VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(
    hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()


@tagged("post_install", "-at_install", "aidoo")
class TestAidooMcpOauth(HttpCase):

    def setUp(self):
        super().setUp()
        self.env["ir.config_parameter"].sudo().set_param("aidoo.mcp_enabled", "1")
        self.password = "aidoo_oauth_pw_123!"
        self.user = self.env["res.users"].create({
            "name": "Aidoo OAuth User", "login": "aidoo_oauth_user",
            "password": self.password,
            "group_ids": [(6, 0, [
                self.env.ref("base.group_user").id,
                self.env.ref("aidoo.group_ai_user").id])],
        })

    # -- helpers ----------------------------------------------------------
    def _register(self, **over):
        body = {"client_name": "Test Client", "redirect_uris": [REDIRECT]}
        body.update(over)
        return self.url_open("/aidoo/oauth/register", data=json.dumps(body),
                             headers={"Content-Type": "application/json"})

    def _client(self, **over):
        res = self._register(**over)
        self.assertEqual(res.status_code, 201, res.text)
        return res.json()

    def _authorize_params(self, client, **over):
        params = {"client_id": client["client_id"], "redirect_uri": REDIRECT,
                  "response_type": "code", "state": "xyz",
                  "code_challenge": CHALLENGE, "code_challenge_method": "S256"}
        params.update(over)
        return params

    def _consent(self, client, decision="allow", login=None, **over):
        """Log in, load the consent page and submit the decision."""
        self.authenticate(login or self.user.login, self.password)
        params = self._authorize_params(client, **over)
        page = self.opener.get(
            self.base_url() + "/aidoo/oauth/authorize", params=params,
            allow_redirects=False)
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn("Test Client", page.text)
        self.assertIn("claude.ai", page.text)
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        data = dict(params, csrf_token=csrf, decision=decision)
        return self.opener.post(self.base_url() + "/aidoo/oauth/authorize",
                                data=data, allow_redirects=False)

    def _code(self, client, **over):
        res = self._consent(client, **over)
        self.assertEqual(res.status_code, 302, res.text)
        query = parse_qs(urlsplit(res.headers["Location"]).query)
        self.assertEqual(query["state"], ["xyz"])
        return query["code"][0]

    def _token(self, client, **data):
        data.setdefault("client_id", client["client_id"])
        return self.opener.post(self.base_url() + "/aidoo/oauth/token",
                                data=data, allow_redirects=False)

    def _exchange(self, client, code, **over):
        data = {"grant_type": "authorization_code", "code": code,
                "code_verifier": VERIFIER, "redirect_uri": REDIRECT}
        data.update(over)
        return self._token(client, **data)

    def _mcp(self, token, method="tools/list"):
        self.opener.cookies.clear()
        return self.url_open(
            "/aidoo/mcp",
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method}),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer %s" % token})

    # -- tests ------------------------------------------------------------
    def test_challenge(self):
        res = self.url_open("/aidoo/mcp", data="{}", headers={
            "Content-Type": "application/json"})
        self.assertEqual(res.status_code, 401)
        header = res.headers["WWW-Authenticate"]
        self.assertIn("resource_metadata=", header)
        self.assertIn("/.well-known/oauth-protected-resource/aidoo/mcp", header)
        self.assertNotIn("invalid_token", header)
        res = self.url_open("/aidoo/mcp", data="{}", headers={
            "Content-Type": "application/json", "Authorization": "Bearer nope"})
        self.assertEqual(res.status_code, 401)
        self.assertIn("invalid_token", res.headers["WWW-Authenticate"])
        self.assertIn("resource_metadata=", res.headers["WWW-Authenticate"])
        self.assertEqual(self.url_open("/aidoo/mcp").status_code, 401)

    def test_metadata(self):
        base = self.base_url()
        res = self.url_open("/.well-known/oauth-protected-resource/aidoo/mcp")
        self.assertEqual(res.status_code, 200)
        doc = res.json()
        self.assertEqual(doc["resource"], base + "/aidoo/mcp")
        self.assertEqual(doc["authorization_servers"], [base + "/aidoo"])
        self.assertEqual(doc["scopes_supported"], ["aidoo_mcp"])
        # RFC 8414 path insertion for issuer <base>/aidoo, plus the appended
        # form some clients try.
        for path in ("/.well-known/oauth-authorization-server/aidoo",
                     "/aidoo/.well-known/oauth-authorization-server"):
            self.assertEqual(self.url_open(path).json()["issuer"],
                             base + "/aidoo", path)
        doc = self.url_open("/.well-known/oauth-authorization-server/aidoo").json()
        self.assertEqual(doc["token_endpoint"], base + "/aidoo/oauth/token")
        self.assertEqual(doc["authorization_endpoint"],
                         base + "/aidoo/oauth/authorize")
        self.assertEqual(doc["registration_endpoint"],
                         base + "/aidoo/oauth/register")
        self.assertEqual(doc["code_challenge_methods_supported"], ["S256"])
        self.assertIn("authorization_code", doc["grant_types_supported"])

    def test_dcr_validation(self):
        client = self._client()
        self.assertEqual(client["redirect_uris"], [REDIRECT])
        self.assertNotIn("client_secret", client)
        self._client(redirect_uris=["http://127.0.0.1:33418/callback"])
        secret = self._client(token_endpoint_auth_method="client_secret_post")
        self.assertTrue(secret["client_secret"])
        stored = self.env["aidoo.oauth.client"].sudo().search(
            [("client_id", "=", secret["client_id"])])
        self.assertNotEqual(stored.secret_hash, secret["client_secret"])
        for bad in ("javascript:alert(1)", "http://example.com/cb",
                    REDIRECT + "#frag", "https://", "ftp://example.com/cb"):
            res = self._register(redirect_uris=[bad])
            self.assertEqual(res.status_code, 400, bad)
            self.assertEqual(res.json()["error"], "invalid_client_metadata")
        self.assertEqual(self._register(redirect_uris=[]).status_code, 400)
        self.assertEqual(self._register(client_name="x" * 500).status_code, 400)

    def test_full_flow(self):
        client = self._client()
        code = self._code(client)
        res = self._exchange(client, code)
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.headers["Cache-Control"], "no-store")
        tokens = res.json()
        self.assertEqual(tokens["token_type"], "Bearer")
        self.assertEqual(tokens["scope"], "aidoo_mcp")
        self.assertLessEqual(tokens["expires_in"], 3600)
        self.assertEqual(self._mcp(tokens["access_token"], "initialize")
                         .status_code, 200)
        listed = self._mcp(tokens["access_token"]).json()
        self.assertIn("tools", listed["result"])
        key = self.env["res.users.apikeys"].sudo().search(
            [("user_id", "=", self.user.id), ("scope", "=", "aidoo_mcp")])
        self.assertTrue(key.name.startswith("Aidoo OAuth"))

    def test_code_rejections(self):
        client = self._client()
        # reuse revokes everything issued from the code
        code = self._code(client)
        tokens = self._exchange(client, code).json()
        self.assertEqual(self._exchange(client, code).status_code, 400)
        self.assertEqual(self._mcp(tokens["access_token"]).status_code, 401)
        # wrong verifier
        res = self._exchange(client, self._code(client),
                             code_verifier="w" * 64)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"], "invalid_grant")
        # redirect mismatch
        res = self._exchange(client, self._code(client),
                             redirect_uri=REDIRECT + "/other")
        self.assertEqual(res.json()["error"], "invalid_grant")
        # expired
        code = self._code(client)
        self.env["aidoo.oauth.grant"].sudo().search([]).write(
            {"code_expires": datetime.now() - timedelta(minutes=1)})
        self.assertEqual(self._exchange(client, code).status_code, 400)
        # unknown grant type / client
        self.assertEqual(self._token(client, grant_type="password")
                         .json()["error"], "unsupported_grant_type")
        res = self._token(client, grant_type="refresh_token",
                          client_id="nope", refresh_token="x")
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json()["error"], "invalid_client")

    def test_confidential_client_secret(self):
        client = self._client(token_endpoint_auth_method="client_secret_post")
        code = self._code(client)
        self.assertEqual(self._exchange(client, code).status_code, 401)
        res = self._exchange(client, code, client_secret=client["client_secret"])
        self.assertEqual(res.status_code, 200, res.text)

    def test_authorize_rejections(self):
        client = self._client()
        self.authenticate(self.user.login, self.password)
        url = self.base_url() + "/aidoo/oauth/authorize"
        for over in ({"client_id": "nope"},
                     {"redirect_uri": "https://evil.example/cb"}):
            res = self.opener.get(url, params=self._authorize_params(client, **over),
                                  allow_redirects=False)
            self.assertEqual(res.status_code, 400)
        res = self.opener.get(url, params=self._authorize_params(
            client, code_challenge_method="plain"), allow_redirects=False)
        self.assertEqual(res.status_code, 302)
        self.assertIn("error=invalid_request", res.headers["Location"])
        # unauthenticated browsers are sent to the login page
        self.opener.cookies.clear()
        res = self.opener.get(url, params=self._authorize_params(client),
                              allow_redirects=False)
        self.assertIn("/web/login", res.headers["Location"])

    def test_non_aidoo_user_and_deny(self):
        client = self._client()
        self.env["res.users"].create({
            "name": "No AI", "login": "aidoo_oauth_noai",
            "password": self.password,
            "group_ids": [(6, 0, [self.env.ref("base.group_user").id])]})
        self.authenticate("aidoo_oauth_noai", self.password)
        res = self.opener.get(
            self.base_url() + "/aidoo/oauth/authorize",
            params=self._authorize_params(client), allow_redirects=False)
        self.assertEqual(res.status_code, 403)
        res = self._consent(client, decision="deny")
        self.assertEqual(res.status_code, 302)
        query = parse_qs(urlsplit(res.headers["Location"]).query)
        self.assertEqual(query["error"], ["access_denied"])
        self.assertEqual(query["state"], ["xyz"])
        self.assertFalse(self.env["aidoo.oauth.grant"].sudo().search([]))

    def test_refresh_rotation_and_reuse(self):
        client = self._client()
        first = self._exchange(client, self._code(client)).json()
        res = self._token(client, grant_type="refresh_token",
                          refresh_token=first["refresh_token"])
        self.assertEqual(res.status_code, 200, res.text)
        second = res.json()
        self.assertNotEqual(second["refresh_token"], first["refresh_token"])
        self.assertEqual(self._mcp(second["access_token"]).status_code, 200)
        self.assertEqual(self._mcp(first["access_token"]).status_code, 401)
        # replaying the rotated token revokes the whole grant
        res = self._token(client, grant_type="refresh_token",
                          refresh_token=first["refresh_token"])
        self.assertEqual(res.json()["error"], "invalid_grant")
        self.assertEqual(self._mcp(second["access_token"]).status_code, 401)
        res = self._token(client, grant_type="refresh_token",
                          refresh_token=second["refresh_token"])
        self.assertEqual(res.status_code, 400)

    def test_disabled(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "aidoo.mcp_enabled", "False")
        for path in ("/.well-known/oauth-protected-resource/aidoo/mcp",
                     "/.well-known/oauth-authorization-server/aidoo",
                     "/aidoo/oauth/authorize?client_id=x"):
            self.assertEqual(self.url_open(path).status_code, 403, path)
        for path in ("/aidoo/oauth/register", "/aidoo/oauth/token"):
            res = self.url_open(path, data="{}")
            self.assertEqual(res.status_code, 403, path)

    def test_bare_well_known_urls_are_left_alone(self):
        """The host-level discovery URLs belong to whatever else on the host
        speaks OAuth; whatever answers there must not be aidoo."""
        for path in ("/.well-known/oauth-protected-resource",
                     "/.well-known/oauth-authorization-server"):
            res = self.url_open(path)
            if res.status_code == 200:
                self.assertNotIn("/aidoo/", res.text, path)

    def test_manual_api_key_still_works(self):
        key = self.env["res.users.apikeys"].with_user(self.user)._generate(
            "aidoo_mcp", "manual", datetime.now() + timedelta(days=1))
        self.assertEqual(self._mcp(key, "ping").status_code, 200)

    def _consent_page(self, client):
        self.authenticate(self.user.login, self.password)
        page = self.opener.get(
            self.base_url() + "/aidoo/oauth/authorize",
            params=self._authorize_params(client), allow_redirects=False)
        self.assertEqual(page.status_code, 200, page.text)
        return page

    def test_consent_forms_carry_explicit_decision(self):
        client = self._client()
        page = self._consent_page(client)
        self.assertNotIn("oe_login_form", page.text)
        forms = [f for f in re.findall(r"<form\b.*?</form>", page.text, re.S)
                 if 'action="/aidoo/oauth/authorize"' in f]
        self.assertEqual(len(forms), 2)
        decisions = []
        for form in forms:
            fields = {}
            for tag in re.findall(r"<input\b[^>]*>", form):
                name = re.search(r'name="([^"]*)"', tag)
                value = re.search(r'value="([^"]*)"', tag)
                if name:
                    fields[name.group(1)] = value.group(1) if value else ""
            decisions.append(fields.get("decision"))
            if fields.get("decision") == "allow":
                allow_fields = fields
        self.assertEqual(sorted(decisions), ["allow", "deny"])
        res = self.opener.post(self.base_url() + "/aidoo/oauth/authorize",
                               data=allow_fields, allow_redirects=False)
        self.assertEqual(res.status_code, 302)
        query = parse_qs(urlsplit(res.headers["Location"]).query)
        self.assertIn("code", query)

    def test_missing_decision_is_not_access_denied(self):
        client = self._client()
        page = self._consent_page(client)
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        data = dict(self._authorize_params(client), csrf_token=csrf)
        res = self.opener.post(self.base_url() + "/aidoo/oauth/authorize",
                               data=data, allow_redirects=False)
        self.assertEqual(res.status_code, 200)
        self.assertIn("No decision was received", res.text)
        self.assertFalse(self.env["aidoo.oauth.grant"].sudo().search([]))

    def test_authorize_route_is_website_compatible(self):
        from odoo.addons.aidoo.controllers.oauth import AidooOauth
        routing = AidooOauth.authorize.original_routing
        self.assertTrue(routing.get("website"))

    def test_oauth_error_page_renders(self):
        self.authenticate(self.user.login, self.password)
        res = self.opener.get(
            self.base_url() + "/aidoo/oauth/authorize?client_id=nope",
            allow_redirects=False)
        self.assertEqual(res.status_code, 400)
        self.assertIn("Cannot authorize", res.text)
