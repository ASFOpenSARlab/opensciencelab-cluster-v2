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


# Does this need to be Async?!?
async def _get_portal_domain(request):
    # Not 100% certain on this syntax here, but looks right based on
    # https://github.com/jupyterhub/jupyterhub/blob/main/jupyterhub/handlers/login.py#L103
    return_path_header = request.headers.get("return-path", None)

    # Check if the return path header is present and in whitelist
    if return_path_header:
        return_path_whitelist = (
            os.environ.get("PORTAL_DOMAINS", "").replace(" ", "").split(",")
        )
        if return_path_header in return_path_whitelist:
            return return_path_header

    # If no return path header, use PORTAL_DOMAINS env var
    osl_portal_domain = (
        os.environ.get("PORTAL_DOMAINS", "").replace(" ", "").split(",")[0]
    )
    if osl_portal_domain:
        return osl_portal_domain

    # OSL Portal Domain could net be determined
    raise My401Exception("No portal domain")


class PortalAuthLoginHandler(BaseHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.LAB_SHORT_NAME = os.environ.get("LAB_SHORT_NAME", "")
        if not self.LAB_SHORT_NAME:
            self.log.error("PortalAuth Login lab name not found")
            raise My401Exception("No lab name")

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
            next = self.get_argument(
                "next", default=f"/lab/{self.LAB_SHORT_NAME}/hub/login"
            )
            next = web.escape.url_escape(next)

            portal_domain = await _get_portal_domain(self.request)
            self.redirect(f"https://{portal_domain}/portal/hub/auth?next_url={next}")

        except My403Exception as e:
            self.log.error(f"PortalAuth Login 403 error: {e}")
            raise web.HTTPError(403)

        except Exception as e:
            self.log.error(f"PortalAuth Login 500 error: {e}")
            raise web.HTTPError(500)


class PortalAuthLogoutHandler(BaseHandler):
    """
    If the user logout of the lab, assume they are logout of Portal.
    The only difference between this class and the original JH is that the logout webpage
    is a redirect to the Portal logout.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.LAB_SHORT_NAME = os.environ.get("LAB_SHORT_NAME", "")
        if not self.LAB_SHORT_NAME:
            self.log.error("PortalAuth Login lab name not found")
            raise My401Exception("No lab name")

    async def post(self):
        raise My401Exception("Not allowed")

    async def get(self):
        portal_domain = await _get_portal_domain(self.request)
        self.redirect(f"https://{portal_domain}/logout", permanent=True)


class PortalAuthenticator(Authenticator):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.LAB_SHORT_NAME = os.environ.get("LAB_SHORT_NAME", "")
        if not self.LAB_SHORT_NAME:
            raise My401Exception("No lab name")

    async def _get_user_data_from_auth_api(self, handler, username: str) -> dict:
        try:
            body = json.dumps(
                {
                    "username": f"{username}",
                    "lab_short_name": self.LAB_SHORT_NAME,
                }
            )
            portal_domain = await _get_portal_domain(handler.request)
            response = await AsyncHTTPClient().fetch(
                f"https://{portal_domain}/portal/hub/auth", body=body, method="POST"
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
            user_data = encryptedjwt.decrypt(response["data"])
        except Exception as e:
            self.log.error(f"PortalAuth Login JWT decryption went wrong: {e}")
            raise My401Exception(
                "Something went wrong with jwt authentication. Contact the administrator."
            )

        return user_data

    async def _get_username_from_username_cookie(self, handler) -> dict:
        encrypted_username: str = handler.get_cookie("portal-username")
        username = encryptedjwt.decrypt(encrypted_username)

        if not username:
            return None

        return {"name": username}

    async def _get_auth_data(self, handler, data: dict = None) -> dict | None:
        if not data:
            data = await self._get_username_from_username_cookie(handler)

        if data:
            username = str(data["name"])

            # Get updated user data from portal
            user_data = await self._get_user_data_from_auth_api(
                handler, username=username
            )

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

                is_admin: bool = user_data.get("admin", False)

                self.log.info(f"Does user '{username}' have admin access? {is_admin}")

                if can_user_access_lab:
                    # Append
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

    async def authenticate(self, handler, data: dict = None) -> dict | None:
        self.log.error("Inside authenticate")
        return await self._get_auth_data(handler, data)

    def get_handlers(self, app):
        return [
            (r"/login", PortalAuthLoginHandler),
            (r"/logout", PortalAuthLogoutHandler),
        ]
