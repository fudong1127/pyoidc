#!/usr/bin/env python

__author__ = 'rohe0002'

import random
import string
import time
import os.path
import json
import urlparse
import httplib2

from urllib import urlencode
from hashlib import md5

from oic.utils import http_util
from oic.oic import Client
from oic.oic.message import AuthorizationRequest
from oic.oic.message import AuthorizationResponse
from oic.oic.message import AccessTokenResponse
from oic.oic.message import ProviderConfigurationResponse
from oic.oic.message import RegistrationRequest
from oic.oic.message import RegistrationResponse
from oic.oauth2.message import ErrorResponse
from oic.oauth2 import Grant
from oic.oauth2.consumer import TokenError
from oic.oauth2.consumer import AuthzError
from oic.oauth2.consumer import UnknownState

SWD_PATTERN = "http://%s/.well-known/simple-web-discovery"
OIDCONF_PATTERN = "%s/.well-known/openid-configuration"
ISSUER_URL = "http://openid.net/specs/connect/1.0/issuer"

def stateID(url, seed):
    """The hash of the time + server path + a seed makes an unique
    SID for each session.

    :param url: The base URL for this site
    :return: The hex version of the digest
    """
    ident = md5()
    ident.update(repr(time.time()))
    ident.update(url)
    ident.update(seed)
    return ident.hexdigest()

def rndstr(size=16):
    """
    Returns a string of random ascii characters or digits

    :param size: The length of the string
    :return: string
    """
    _basech = string.ascii_letters + string.digits
    return "".join([random.choice(_basech) for _ in range(size)])

def factory(kaka, sdb, config):
    """
    Return the right Consumer instance dependent on what's in the cookie

    :param kaka: The cookie
    :param sdb: The session database
    :param config: The common Consumer configuration
    :return: Consumer instance or None
    """
    part = http_util.cookie_parts(config["name"], kaka)
    if part is None:
        return None

    cons = Consumer(sdb, config=config)
    cons.restore(part[0])
    http_util.parse_cookie(config["name"], cons.seed, kaka)
    return cons

PARAMS = ["client_id","state","grant", "redirect_uri",
          "authorization_endpoint", "token_endpoint",
          "token_revocation_endpoint", "user_info_endpoint", "seed", "debug",
          "nonce", "request_filename", "user_info", "id_token"]

class Consumer(Client):
    """ An OpenID Connect consumer implementation

    """
    #noinspection PyUnusedLocal
    def __init__(self, session_db, config, client_config=None,
                 server_info=None):
        """ Initializes a Consumer instance.

        :param session_db: Where info are kept about sessions
        :param config: Configuration of the consumer
        :param client_config: Client configuration
        :param server_info: Information about the server
        """
        if client_config is None:
            client_config = {}

        Client.__init__(self, **client_config)

        self.config = config
        if config:
            self.debug = config["debug"]

        if server_info:
            self.authorization_endpoint = server_info["authorization_endpoint"]
            self.token_endpoint = server_info["token_endpoint"]
            self.user_info_endpoint = server_info["user_info_endpoint"]

        self.sdb = session_db
        try:
            self.function = self.config["function"]
        except (KeyError, TypeError):
            self.function = {}
            
        self.seed = ""
        self.nonce = ""
        self.request_filename=""
        self.user_info = None
        self.registration_expires_in = 0

    def update(self, sid):
        """ Updates the instance variables from something stored in the
        session database. Will not overwrite something that's already there.
        Except for the grant dictionary !!

        :param sid: Session identifier
        """
        for key, val in self.sdb[sid].items():
            _val = getattr(self, key)
            if not _val and val:
                setattr(self, key, val)
            elif key == "grant" and val:
                val.update(_val)
                setattr(self, key, val)

    def restore(self, sid):
        """ Restores the instance variables from something stored in the
        session database.

        :param sid: Session identifier
        """
        for key, val in self.sdb[sid].items():
            setattr(self, key, val)

    def grant_from_state(self, state):
        res = Client.grant_from_state(self, state)
        if res:
            return res

        try:
            session = self.sdb[state]
        except KeyError:
            return None

        for scope, grant in session["grant"].items():
            if grant.state == state:
                self.grant[scope] = grant
                return grant

        return None

    def dictionary(self):
        return dict([(p, getattr(self,p)) for p in PARAMS])

    def _backup(self, sid):
        """ Stores instance variable values in the session store under a
        session identifier.

        :param sid: Session identifier
        """
        self.sdb[sid] = self.dictionary()

    def extract_access_token_response(self, aresp):
        atr = AccessTokenResponse()
        for prop in AccessTokenResponse.c_attributes.keys():
            setattr(atr, prop, getattr(aresp, prop))

        # update the grant object
        self.grant_from_state(aresp.state).add_token(atr)
        
        return atr
    
    #noinspection PyUnusedLocal,PyArgumentEqualDefault
    def begin(self, environ, start_response, logger, scope="", response_type=""):
        """ Begin the OAuth2 flow

        :param environ: The WSGI environment
        :param start_response: The function to start the response process
        :param logger: A logger instance
        :return: A URL to which the user should be redirected
        """
        _log_info = logger.info

        if self.debug:
            _log_info("- begin -")

        _path = http_util.geturl(environ, False, False)
        self.redirect_uri = _path + self.config["authz_page"]

        # Put myself in the dictionary of sessions, keyed on session-id
        if not self.seed:
            self.seed = rndstr()

        if not scope:
            scope = self.config["scope"]
        if not response_type:
            response_type = self.config["response_type"]

        sid = stateID(_path, self.seed)
        self.state = sid
        self.grant[sid] = Grant()

        self._backup(sid)
        self.sdb["seed:%s" % self.seed] = sid

        # Store the request and the redirect uri used
        self._request = http_util.geturl(environ)
        self.nonce = rndstr(12)

        args = {
            "state":sid,
            "response_type":response_type,
            "scope": scope,
            "nonce": self.nonce
        }
        
        areq = self.construct_AuthorizationRequest(AuthorizationRequest,
                                                   request_args=args)

        id_request = self.function["openid_request"](areq, self.config["key"])
        if self.config["request_method"] == "parameter":
            areq.request = id_request
        elif self.config["request_method"] == "simple":
            pass
        else: # has to be 'file' at least that's my assumption.
            # write to file in the tmp directory remember the name
            filename = os.path.join(self.config["temp_dir"], rndstr(10))
            while os.path.exists(filename):
                filename = os.path.join(self.config["temp_dir"], rndstr(10))
            fid = open(filename)
            fid.write(id_request)
            fid.close()
            self.request_filename = "/"+filename
            self._backup(sid)

        location = "%s?%s" % (self.authorization_endpoint,
                              areq.get_urlencoded())

        if self.debug:
            _log_info("Redirecting to: %s" % location)

        return location

    #noinspection PyUnusedLocal
    def parse_authz(self, environ, start_response, logger):
        """
        This is where we get redirect back to after authorization at the
        authorization server has happened.

        :param environ: The WSGI environment
        :param start_response: The function to start the response process
        :param logger: A logger instance
        :return: A AccessTokenResponse instance
        """

        _log_info = logger.info
        if self.debug:
            _log_info("- authorization -")
            _log_info("- %s flow -" % self.config["flow_type"])
            _log_info("environ: %s" % environ)

        if environ.get("REQUEST_METHOD") == "GET":
            _query = environ.get("QUERY_STRING")
        elif environ.get("REQUEST_METHOD") == "POST":
            _query = http_util.get_post(environ)
        else:
            resp = http_util.BadRequest("Unsupported method")
            return resp(environ, start_response)

        _log_info("response: %s" % _query)
        
        _path = http_util.geturl(environ, False, False)

        if "code" in self.config["response_type"]:
            # Might be an error response
            _log_info("Expect Authorization Response")
            aresp = self.parse_response(AuthorizationResponse, info=_query)
            if isinstance(aresp, ErrorResponse):
                _log_info("ErrorResponse: %s" % aresp)
                raise AuthzError(aresp.error)

            _log_info("Aresp: %s" % aresp)

            try:
                self.update(aresp.state)
            except KeyError:
                raise UnknownState(aresp.state)

            self.redirect_uri = self.sdb[aresp.state]["redirect_uri"]

            # May have token and id_token information too
            if aresp.access_token:
                atr = self.extract_access_token_response(aresp)
                self.access_token = atr
            else:
                atr = None

            self._backup(aresp.state)

            idt = None
            return aresp, atr, idt
        else: # implicit flow
            _log_info("Expect Access Token Response")
            atr = self.parse_response(AccessTokenResponse, info=_query,
                                      format="urlencoded", extended=True)
            if isinstance(atr, ErrorResponse):
                raise TokenError(atr.error)

            idt = None
            return None, atr, idt

    def complete(self, logger):
        """
        Do the access token request, the last step in a code flow.
        If Implicit flow was used then this method is never used.
        """
        if self.config["password"]:
            logger.info("basic auth")
            args = {"client_password":self.config["password"]}
            atr = self.construct_AccessTokenRequest(request_args=args,
                                                    state=self.state)
        elif self.client_secret:
            logger.info("request_body auth")
            args = {"client_secret":self.config["client_secret"],
                    "client_id": self.client_id,
                    "auth_method":"request_body"}
            atr = self.construct_AccessTokenRequest(request_args=args,
                                                    state=self.state)
        else:
            raise Exception("Nothing to authenticate with")

        logger.info("Access Token Response: %s" % atr)

        if isinstance(atr, ErrorResponse):
            raise TokenError(atr.error)

        #self._backup(self.sdb["seed:%s" % _cli.seed])
        self._backup(self.state)

        return atr

    def refresh_token(self):
        pass
    
    #noinspection PyUnusedLocal
    def userinfo(self, logger):
        self.log = logger
        uinfo = self.do_user_info_request()

        if isinstance(uinfo, ErrorResponse):
            raise TokenError(uinfo.error)

        self.user_info = uinfo
        self._backup(self.state)

        return uinfo

    def refresh_session(self):
        pass

    def check_session(self):
        pass

    def end_session(self):
        pass

    def issuer_query(self, location, principal):
        param = {
            "service": ISSUER_URL,
            "principal": principal,
        }

        return "%s?%s" % (location, urlencode(param))

    def _disc_query(self, uri, principal):
        try:
            (response, content) = self.http.request(uri)
        except httplib2.ServerNotFoundError:
            if uri.startswith("http://"): # switch to https
                location = "https://%s" % uri[7:]
                return self._disc_query(location, principal)
            else:
                raise

        if response.status == 200:
            result = json.loads(content)
            if "SWD_service_redirect" in result:
                _uri = self.issuer_query(
                            result["SWD_service_redirect"]["locations"][0],
                            principal)
                return self._disc_query(_uri, principal)
            else:
                return result
        else:
            raise Exception(response.status)

    def provider_config(self, issuer):

        url = OIDCONF_PATTERN % issuer

        (response, content) = self.http.request(url)
        if response.status == 200:
            return ProviderConfigurationResponse.from_json(content)
        else:
            raise Exception("%s" % response.status)

    def get_domain(self, principal, idtype="mail"):
        if idtype == "mail":
            (local, domain) = principal.split("@")
        elif idtype == "url":
            domain, user = urlparse.urlparse(principal)[1:2]
        else:
            domain = ""

        return domain
    
    def discover(self, principal, idtype="mail"):
        domain = self.get_domain(principal, idtype)
        uri = self.issuer_query(SWD_PATTERN % domain, principal)

        result = self._disc_query(uri, principal)

        try:
            return self.provider_config(result["locations"][0])
        except Exception:
            return result["location"]

    def register(self, server, type="client_associate", **kwargs):
        req = RegistrationRequest(type=type)

        if type == "client_update":
            req.client_id = self.client_id
            req.client_secret = self.client_secret

        for prop in RegistrationRequest.c_attributes.keys():
            if prop in ["type", "client_id", "client_secret"]:
                continue
            if prop in kwargs:
                setattr(req, prop, kwargs[prop])

        print "2",req.to_urlencoded()

        headers = {"content-type": "application/x-www-form-urlencoded"}
        (response, content) = self.http.request(server, "POST",
                                                req.to_urlencoded(),
                                                headers=headers)

        if response.status == 200:
            resp = RegistrationResponse.from_json(content)
            self.client_secret = resp.client_secret
            self.client_id = resp.client_id
            self.registration_expires_in = resp.expires_in
        else:
            raise Exception("Registration failed: %s" % response.status)
        