import os
import json
import random
import string
import requests
import time
import smtplib
import threading
import werkzeug
import jwt
import datetime
import copy
import re
import shutil

from flask import (Flask, request, redirect, render_template, send_file,
                   session, Markup, escape)

from flask_sqlalchemy import SQLAlchemy
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps

vars = json.loads(open("vars.json").read())

smtp_url = vars["smtp_url"]
email_addr = vars["email_addr"]
email_pass = vars["email_pass"]
owner_secret = vars["owner_secret"]
session_secret = vars["session_secret"]
captcha_v3 = vars["captcha_v3"]
captcha_v2 = vars["captcha_v2"]

ratelimit_cache = []
register_cache = []
verify_cache = []

threads = []

flask = Flask(__name__, template_folder="./html")
flask.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024
flask.config["SECRET_KEY"] = session_secret
flask.config["SESSION_COOKIE_SAMESITE"] = "Strict"
flask.config["SQLALCHEMY_DATABASE_URI"] = ("mysql+pymysql://"
                                           "accounts@localhost:3306/accounts")

flask.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
exts = ["jpg", "png"]
db = SQLAlchemy(flask)


class User(db.Model):
    id = db.Column(db.BigInteger, primary_key=True)
    name = db.Column(db.String(32), unique=True, nullable=False)
    email = db.Column(db.String(64), unique=True, nullable=False)
    password = db.Column(db.String(256), nullable=False)
    token = db.Column(db.String(256), unique=True, nullable=False)
    token_secret = db.Column(db.String(256), unique=True, nullable=False)
    twofa_secret = db.Column(db.String(256), unique=True)
    recovery_token = db.Column(db.String(256), unique=True)
    permission = db.Column(db.Integer, nullable=False)
    verified = db.Column(db.Boolean, nullable=False)
    banned = db.Column(db.Boolean, nullable=False)
    ban_expiry = db.Column(db.BigInteger)
    ban_reason = db.Column(db.String(200))


"""
class UserRelationship(db.Model):
    user_id = db.Column(db.BigInteger, db.ForeignKey(User.id), nullable=False)
    id = db.Column(db.BigInteger, db.ForeignKey(User.id), primary_key=True)
    status = db.Column(db.Integer, nullable=False)

    user = db.relationship("User", foreign_keys="UserRelationship.user_id")
    recipient = db.relationship("User", foreign_keys="UserRelationship.id")
"""


class App(db.Model):
    id = db.Column(db.BigInteger, primary_key=True)
    owner_id = db.Column(db.BigInteger, db.ForeignKey(User.id), nullable=False)
    secret = db.Column(db.String(256), unique=True, nullable=False)
    name = db.Column(db.String(32), nullable=False)
    callback = db.Column(db.String(1024), nullable=False)
    website = db.Column(db.String(64))
    approved = db.Column(db.Boolean, nullable=False)
    verified = db.Column(db.Boolean, nullable=False)

    owner = db.relationship("User", foreign_keys="App.owner_id")


class AppUser(db.Model):
    app_id = db.Column(db.BigInteger, db.ForeignKey(App.id), nullable=False)
    id = db.Column(db.BigInteger, db.ForeignKey(User.id), primary_key=True)
    token = db.Column(db.String(256), unique=True, nullable=False)

    user = db.relationship("User", foreign_keys="AppUser.id")
    app = db.relationship("App", foreign_keys="AppUser.app_id")


class IP:
    def __init__(self, address):
        if not address:
            self.address = None
            self.location = None
        else:
            self.address = address

            response = requests.get(f"http://ip-api.com/json/{self.address}")
            data = response.json()
            if not data.get("city") or not data.get("regionName"):
                self.location = None
            else:
                self.location = (data.get("city")
                                 + ", "
                                 + data.get("regionName"))


db.create_all()


def email_send(email, subject, body):
    def email_wrapper(email, subject, body):
        with smtplib.SMTP(smtp_url, 587) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()

            smtp.login(email_addr, email_pass)

            msg = f"Subject: {subject}\n\n{body}"

            smtp.sendmail(email_addr, email, msg)

    email_thread = threading.Thread(target=email_wrapper,
                                    kwargs={"email": email,
                                            "subject": subject,
                                            "body": body})
    email_thread.start()

    return


def gen_id(type, table=None, field=None):
    id = (str(datetime.date.today().year - 2000)
          + datetime.datetime.now().strftime("%m%d")
          + str(type)
          + "".join(random.choices(string.digits, k=5)))

    if table and field:
        while table.query.filter_by(**{field: id}).first():
            id = (str(datetime.date.today().year - 2000)
                  + datetime.datetime.now().strftime("%m%d")
                  + str(type)
                  + "".join(random.choices(string.digits, k=5)))

    return id


def gen_token(table=None, field=None):
    token = "".join(random.choices(string.ascii_letters
                                   + string.digits, k=256))
    if table and field:
        while table.query.filter_by(**{field: token}).first():
            token = "".join(random.choices(string.ascii_letters
                                           + string.digits, k=256))

    return token


def user_asdict(user, deep=False):
    user_dict = copy.copy(user.__dict__)

    user_dict.pop("_sa_instance_state")
    user_dict.pop("email")
    user_dict.pop("password")
    user_dict.pop("token")
    user_dict.pop("token_secret")
    user_dict.pop("twofa_secret")
    user_dict.pop("recovery_token")
    user_dict.pop("ban_reason")
    user_dict.pop("ban_expiry")

    if deep:
        user_dict["apps"] = {"own": [], "use": []}

        for app in App.query.filter_by(owner_id=user.id).all():
            user_dict["apps"]["own"].append(app_asdict(app))

        for app_user in AppUser.query.filter_by(id=user.id).all():
            user_dict["apps"]["use"].append(app_asdict(app_user.app))

    return user_dict


def app_asdict(app):
    app_dict = copy.copy(app.__dict__)

    app_dict.pop("_sa_instance_state")
    app_dict.pop("secret")

    app_dict["owner"] = user_asdict(app.owner)

    return app_dict


def session_key(key,
                min: int = 1,
                max: int = 4096,
                var_type: type = str,
                required: bool = True):

    def wrapper(f):
        @wraps(f)
        def wrapper_function(*args, **kwargs):
            value = session.get(key)
            if not value and required:
                return {"text": f"Please specify a value for '{key}'!",
                        "error": f"invalid_{key}"}, 400
            elif not required:
                value = value or None

            if value:
                if not isinstance(value, var_type):
                    return {"text": (f"Value for '{key}' must be type "
                                     f"{var_type.__name__}!"),
                            "error": f"invalid_{key}"}, 400

                if len(str(value)) < min:
                    return {"text": (f"Value for '{key}' must be at least "
                                     f"{min} characters!"),
                            "error": f"invalid_{key}"}, 400

                if len(str(value)) > max:
                    return {"text": (f"Value for '{key}' must be at most "
                                     f"{max} characters!"),
                            "error": f"invalid_{key}"}, 400

            return f(**{key: value}, **kwargs)
        return wrapper_function
    return wrapper


def json_key(key,
             min: int = 1,
             max: int = 4096,
             var_type: type = str,
             required: bool = True):

    def wrapper(f):
        @wraps(f)
        def wrapper_function(*args, **kwargs):
            if request.json:
                value = request.json.get(key)
                if not value and required:
                    return {"text": f"Please specify a value for '{key}'!",
                            "error": f"invalid_{key}"}, 400
                elif not required:
                    value = value or None
            else:
                if required:
                    return {"text": "Bad request!",
                            "error": "bad_request"}, 400
                else:
                    value = None

            if value:
                if not isinstance(value, var_type):
                    return {"text": (f"Value for '{key}' must be type "
                                     f"{var_type.__name__}!"),
                            "error": f"invalid_{key}"}, 400

                if len(str(value)) < min:
                    return {"text": (f"Value for '{key}' must be at least "
                                     f"{min} characters!"),
                            "error": f"invalid_{key}"}, 400

                if len(str(value)) > max:
                    return {"text": (f"Value for '{key}' must be at most "
                                     f"{max} characters!"),
                            "error": f"invalid_{key}"}, 400

            return f(**{key: value}, **kwargs)
        return wrapper_function
    return wrapper


def args_key(key,
             min: int = 1,
             max: int = 4096,
             var_type: type = str,
             required: bool = True):

    def wrapper(f):
        @wraps(f)
        def wrapper_function(*args, **kwargs):
            if request.args:
                value = request.args.get(key)
                if not value and required:
                    return {"text": f"Please specify a value for '{key}'!",
                            "error": f"invalid_{key}"}, 400
                elif not required:
                    value = value or None
            else:
                if required:
                    return {"text": "Bad request!",
                            "error": "bad_request"}, 400
                else:
                    value = None

            if value:
                if not isinstance(value, var_type):
                    try:
                        value = var_type(value)
                    except ValueError:
                        return {"text": (f"Value for '{key}' must be type "
                                         f"{var_type.__name__}!"),
                                "error": f"invalid_{key}"}, 400

                if len(str(value)) < min:
                    return {"text": (f"Value for '{key}' must be at least "
                                     f"{min} characters!"),
                            "error": f"invalid_{key}"}, 400

                if len(str(value)) > max:
                    return {"text": (f"Value for '{key}' must be at most "
                                     f"{max} characters!"),
                            "error": f"invalid_{key}"}, 400

            return f(**{key: value}, **kwargs)
        return wrapper_function
    return wrapper


def captcha2(f):
    @wraps(f)
    @json_key("captcha")
    def wrapper_function(captcha, *args, **kwargs):
        try:
            response = requests.post(("https://www.google.com"
                                     "/recaptcha/api/siteverify"),
                                     data={"secret": captcha_v2,
                                           "response": captcha},
                                     timeout=5)

        except requests.exceptions.Timeout:
            return f(*args, **kwargs)

        data = response.json()
        if not data["success"]:
            return {"text": "Invalid captcha response!",
                    "error": "invalid_captcha_response"}, 401

        return f(*args, **kwargs)
    return wrapper_function


def captcha3(f):
    @wraps(f)
    @json_key("captcha")
    def wrapper_function(captcha, *args, **kwargs):
        try:
            response = requests.post(("https://www.google.com"
                                     "/recaptcha/api/siteverify"),
                                     data={"secret": captcha_v3,
                                           "response": captcha},
                                     timeout=5)

        except requests.exceptions.Timeout:
            return f(*args, **kwargs)

        data = response.json()
        if not data["success"]:
            return {"text": "Invalid captcha response!",
                    "error": "invalid_captcha_response"}, 401

        return f(*args, **kwargs)
    return wrapper_function


def auth(required: bool = True,
         redirect_url: str = None,
         redirect_back: str = None):

    def wrapper(f):
        @wraps(f)
        @session_key("token", 1, 256, required=False)
        @args_key("response_type", required=False)
        @args_key("app_id", 12, 12, int, required=False)
        def wrapper_function(token, response_type, app_id, *args, **kwargs):
            if not token and required:
                if redirect_url:
                    if response_type and app_id:
                        return redirect(redirect_url
                                        + f"?response_type={response_type}"
                                        + f"&app_id={app_id}",
                                        302)
                    else:
                        if redirect_back:
                            return redirect(redirect_url
                                            + f"?redirect_url={redirect_back}",
                                            302)
                        else:
                            return redirect(redirect_url, 302)
                else:
                    return {"text": "Please specify a value for 'token'!",
                            "error": "invalid_token"}, 400

            if token:
                account = User.query.filter_by(token=token).first()
            else:
                account = None

            if not account and required:
                if redirect_url:
                    if response_type and app_id:
                        return redirect(redirect_url
                                        + f"?response_type={response_type}"
                                        + f"&app_id={app_id}",
                                        302)
                    else:
                        return redirect(redirect_url, 302)
                else:
                    return {"text": "Token does not exist!",
                            "error": "invalid_token"}, 401

            if account:
                if account.banned:
                    if not redirect_url:
                        return {"text": "Account is banned!",
                                "error": "account_banned"}, 403
                    else:
                        return redirect("/error?code=account_banned", 302)

                elif not account.verified:
                    if not redirect_url:
                        return {"text": "Account is not verified!",
                                "error": "account_unverified"}, 403
                    else:
                        return redirect("/error?code=account_unverified", 302)

            return f(**{"account": account}, **kwargs)
        return wrapper_function
    return wrapper


def no_auth(redirect_url: str = None):
    def wrapper(f):
        @wraps(f)
        @session_key("token", 1, 256, required=False)
        @args_key("response_type", required=False)
        @args_key("app_id", 12, 12, int, required=False)
        def wrapper_function(token, response_type, app_id, *args, **kwargs):
            account = User.query.filter_by(token=token).first()
            if account:
                if account.banned:
                    if not redirect_url:
                        return {"text": "Account is banned!",
                                "error": "account_banned"}, 403
                    else:
                        return redirect("/error?code=account_banned", 302)

                if redirect_url:
                    if response_type and id:
                        return redirect("/oauth/authorize"
                                        + f"?response_type={response_type}"
                                        + f"&app_id={app_id}",
                                        302)
                    else:
                        return redirect(redirect_url, 302)
                else:
                    return {"text": "You are logged in!",
                            "error": "logged_in"}, 403

            return f(*args, **kwargs)
        return wrapper_function
    return wrapper


def ratelimit(f):
    @wraps(f)
    def wrap(*args, **kwargs):
        ip = IP(request.headers.get("CF-Connecting-IP"))

        timestamp = time.time()
        for user in ratelimit_cache:
            if ip.address == user["ip"]:
                if user["timestamp"] > timestamp:
                    return {"text": "You are being ratelimited!",
                            "error": "ratelimit"}, 429
                else:
                    user["timestamp"] = timestamp + 3
                    return f(*args, **kwargs)

        ratelimit_cache.append({"ip": ip.address, "timestamp": timestamp + 3})

        return f(*args, **kwargs)
    return wrap


def tasks():
    while 1:
        timestamp = time.time()
        for user in User.query.filter_by(banned=True).all():
            if user.ban_expiry and timestamp > user.ban_expiry:
                user.banned = False
                user.ban_expiry = None
                user.ban_reason = None

                for app_user in AppUser.query.filter_by(id=user.id).all():
                    app_user.token = jwt.encode(user_asdict(user),
                                                app_user.app.secret,
                                                algorithm="HS256")

                db.session.commit()

                subject = "Ban Notice"
                body = (f"Hello {escape(user.name)}!\n\n"
                        "Your account was unbanned.\n\n"
                        "Please read our Terms of Service and "
                        "refer to your ban reason to avoid future bans.\n\n"
                        "We hope you understand. Thanks for using Connext!")

                email_send(user.email, subject, body)

        time.sleep(21600)


task_thread = threading.Thread(target=tasks)
task_thread.start()
