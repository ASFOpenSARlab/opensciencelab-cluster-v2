import os
import json
import traceback

from tornado import web
from tornado.httpclient import AsyncHTTPClient
from jupyterhub.auth import Authenticator
from jupyterhub.handlers import BaseHandler

from opensarlab.auth import encryptedjwt


class My403Exception(Exception):
    pass


class My401Exception(Exception):
    pass


class PortalAuthLoginHandler(BaseHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.lab_prefix = os.environ.get("JUPYTERHUB_LAB_PREFIX", "")
        if not self.lab_prefix:
            raise My401Exception("No lab prefix")

        portal_domains = os.environ.get("PORTAL_DOMAINS", "")
        if not portal_domains:
            raise My401Exception("No portal domains")

        # Assume logging out of the primary portal since we don't know which portal was used to login
        self.primary_portal_domain = portal_domains.split(",")[0].strip()

    async def post(self):
        raise My401Exception("Not allowed")

    async def get(self):
        """
        If current JupyterHub user not found, login user via redirect.
        If current JupyterHub user found (and signed in), set Lab JupyterHub cookie and redirect back to original url.
        """
        try:
            self.statsd.incr("login.request")
            user = self.current_user
            if user:
                # set new login cookie
                # because single-user cookie may have been cleared or incorrect
                self.set_login_cookie(user)
                self.redirect(self.get_next_url(user), permanent=False)
            else:
                user = await self.login_user()
                if user is None:
                    raise My403Exception(
                        f"login_user function failed for user '{user}'"
                    )
                else:
                    self.redirect(self.get_next_url(user))

        except My401Exception as e:
            self.log.error(f"PortalAuth Login 401 error: {e}")
            next = self.get_argument("next", default=f"{self.lab_prefix}/hub/login")
            next = web.escape.url_escape(next)

            self.redirect(
                f"{self.primary_portal_domain}/portal/hub/auth?next_url={next}"
            )

        except My403Exception as e:
            self.log.error(f"PortalAuth Login 403 error: {e}")
            raise web.HTTPError(403)

        except Exception as e:
            self.log.error(f"PortalAuth Login 500 error: {e}")
            self.log.error(f"PortalAuth: Traceback: {traceback.format_exc()}")
            raise web.HTTPError(500)


class PortalAuthLogoutHandler(BaseHandler):
    """
    If the user logout of the lab, assume they are logout of Portal.
    The only difference between this class and the original JH is that the logout webpage
    is a redirect to the Portal logout.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        portal_domains = os.environ.get("PORTAL_DOMAINS", "")
        if not portal_domains:
            raise My401Exception("No portal domains")

        # Assume logging out of the primary portal since we don't know which portal was used to login
        self.primary_portal_domain = portal_domains.split(",")[0].strip()

    async def render_logout_page(self):
        self.redirect(f"{self.primary_portal_domain}/logout", permanent=True)


class PortalAuthenticator(Authenticator):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.LAB_SHORT_NAME = os.environ.get("LAB_SHORT_NAME", "")
        if not self.LAB_SHORT_NAME:
            raise My401Exception("No lab name provided")

        portal_domains = os.environ.get("PORTAL_DOMAINS", "")
        if not portal_domains:
            raise My401Exception("No portal domains")

        self.primary_portal_domain = portal_domains.split(",")[0].strip()

    async def _get_user_data_from_auth_api(self, username: str) -> dict:
        try:
            body = json.dumps(
                {"username": f"{username}", "lab_short_name": self.LAB_SHORT_NAME}
            )
            response = await AsyncHTTPClient().fetch(
                f"{self.primary_portal_domain}/portal/hub/auth",
                body=body,
                method="POST",
            )

            if not response.code == 200:
                self.log.error(
                    f"Auth response code is not 200. Code: {response.code}, {response['message']}"
                )
                raise My401Exception()

            response = json.loads(response.body)
            if "ERROR" in response["message"]:
                self.log.error(f"{response['message']}")
                raise My401Exception()

        except Exception as e:
            self.log.error(f"Something went wrong with retrieving authentication. {e}")
            raise My401Exception()

        try:
            return encryptedjwt.decrypt(response["data"])
        except Exception as e:
            self.log.error(f"PortalAuth Login JWT decryption went wrong: {e}")
            raise My401Exception(
                "Something went wrong with jwt authentication. Contact the administrator."
            )

    async def _get_username_from_username_cookie(self, handler) -> dict:
        encrypted_username: str = handler.get_cookie("portal-username")

        # If the user has no username cookie, their session has expired
        if encrypted_username is None:
            raise My401Exception("User has no `portal-username` cookie")

        username = encryptedjwt.decrypt(encrypted_username)

        self.log.info(f"Username '{username}' got from 'portal-username' cookie.")

        if not username:
            return {}

        return {"name": username}

    async def _get_auth_data(self, handler, data: dict = {}) -> dict | None:
        if not data:
            data = await self._get_username_from_username_cookie(handler)

        if data:
            username = str(data["name"])

            # Get updated user data from portal
            user_data: dict = await self._get_user_data_from_auth_api(username=username)

            if user_data is None:
                self.log.error("No JWT data found")
                raise My401Exception("No jwt data")

            self.log.warning(
                f"Cheap writers killed Data like Khan in Nemesis. User data: {user_data}"
            )
            try:
                user_data_access_for_lab: dict = user_data.get("lab_access", {}).get(
                    self.LAB_SHORT_NAME, {}
                )
                if not user_data_access_for_lab:
                    return None

                self.log.info(
                    f"User data access for lab '{self.LAB_SHORT_NAME}': {user_data_access_for_lab}"
                )

                can_user_access_lab: bool = bool(
                    user_data_access_for_lab.get("can_user_access_lab", False)
                )

                self.log.info(
                    f"Can user access lab '{self.LAB_SHORT_NAME}'? {can_user_access_lab}"
                )

                user_data_roles: list = user_data.get("roles", [])
                is_admin: bool = "admin" in user_data_roles

                self.log.info(f"Does user '{username}' have admin access? {is_admin}")

                if can_user_access_lab:
                    return_path = handler.request.headers.get("return-path", None)
                    return {
                        "name": username,
                        "admin": is_admin,
                        "auth_state": {
                            "return_portal": return_path,
                        },
                    }

            except Exception:
                self.log.error(f"Portal Auth: Traceback: {traceback.format_exc()}")

        return None

    async def authenticate(self, handler, data: dict = {}) -> dict | None:
        self.log.error("Inside authenticate")
        return await self._get_auth_data(handler, data)

    def get_handlers(self, app):
        return [
            (r"/login", PortalAuthLoginHandler),
            (r"/logout", PortalAuthLogoutHandler),
        ]
