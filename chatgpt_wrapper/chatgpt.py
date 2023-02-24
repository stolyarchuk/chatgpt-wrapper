import atexit
import base64
import json
import logging
import operator
import re
import shutil
import time
import uuid
from functools import reduce
from json.decoder import JSONDecodeError
from time import sleep
from typing import Any, Optional

from playwright._impl._api_structures import ProxySettings
from playwright._impl._fetch import APIResponse
from playwright.sync_api import sync_playwright

RENDER_MODELS = {
    "default": "text-davinci-002-render-sha",
    "legacy-paid": "text-davinci-002-render-paid",
    "legacy-free": "text-davinci-002-render"
}

log_formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")


def remove_json_invalid_control_chars(s: str):
    """
    Removes invalid control characters from the given string.
    """
    # Define a regex pattern that matches invalid control characters
    pattern = re.compile(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F\n]')

    # Use the pattern to remove invalid control characters from the string
    return pattern.sub('', s)


class ChatGPT:
    """
    A ChatGPT interface that uses Playwright to run a browser,
    and interacts with that browser to communicate with ChatGPT in
    order to provide an open API to ChatGPT.
    """

    stream_div_id = "chatgpt-wrapper-conversation-stream-data"
    eof_div_id = "chatgpt-wrapper-conversation-stream-data-eof"
    session_div_id = "chatgpt-wrapper-session-data"

    def __init__(self,
                 headless: bool = True,
                 browser: str = "firefox",
                 model: str = "default",
                 timeout: int = 60,
                 debug_log: Optional[str] = None,
                 proxy: Optional[ProxySettings] = None):
        self.log = self._set_logging(debug_log)
        self.log.debug("ChatGPT initialized")
        self.play = sync_playwright().start()
        try:
            playbrowser = getattr(self.play, browser)
        except Exception:
            print(f"Browser {browser} is invalid, falling back on firefox")
            playbrowser = self.play.firefox
        try:
            self.browser = playbrowser.launch_persistent_context(
                user_data_dir="/tmp/playwright",
                headless=headless,
                proxy=proxy,
            )
        except Exception:
            self.user_data_dir = f"/tmp/{str(uuid.uuid4())}"
            shutil.copytree("/tmp/playwright", self.user_data_dir)
            self.browser = playbrowser.launch_persistent_context(
                user_data_dir=self.user_data_dir,
                headless=headless,
                proxy=proxy,
            )

        if len(self.browser.pages) > 0:
            self.page = self.browser.pages[0]
        else:
            self.page = self.browser.new_page()
        self._start_browser()
        self.parent_message_id = str(uuid.uuid4())
        self.conversation_id: Optional[str] = None
        self.conversation_title_set = None
        self.session: dict[str, str] = dict()
        self.model = model
        self.timeout = timeout
        atexit.register(self._cleanup)

    def switch_to_conversation(self, conversation_id: str):
        self.conversation_id = conversation_id
        conversation_info = self.get_conversation_info(conversation_id)

        if conversation_info:
            self.parent_message_id = conversation_info["current_node"]

    def get_conversation_info(self, conversation_id: str) -> Optional[dict[str, Any]]:
        if not self.session:
            self.refresh_session()

        if "accessToken" not in self.session:
            print("Your ChatGPT session is not usable.\n"
                  "* Run this program with the `install` parameter and log in to ChatGPT.\n"
                  "* If you think you are already logged in, try running the `session` command.")
            return

        conversation_div_id = "chatgpt-wrapper-conversation-info-data"

        code = ("""
            const xhr = new XMLHttpRequest();
            xhr.open('GET', 'https://chat.openai.com/backend-api/conversation/CONVERSATION_ID');
            xhr.setRequestHeader('Accept', 'text/event-stream');
            xhr.setRequestHeader('Content-Type', 'application/json');
            xhr.setRequestHeader('Authorization', 'Bearer BEARER_TOKEN');
            xhr.responseType = 'stream';
            xhr.onload = function() {
                console.log('get conversation info status:'+xhr.status);
                if(xhr.status == 200) {
                    var conversation_info_div = document.createElement('DIV');
                    conversation_info_div.id = "CONVERSATION_INFO_DIV_ID";
                    conversation_info_div.innerHTML = xhr.responseText;
                    document.body.appendChild(conversation_info_div);
                }
            };
            xhr.send();
            """.replace("BEARER_TOKEN", self.session["accessToken"]).replace(
            "CONVERSATION_INFO_DIV_ID", conversation_div_id).replace("CONVERSATION_ID", conversation_id))
        self.page.evaluate(code)
        conversation_info = None

        while True:
            conversation_info_datas = self.page.query_selector_all(f"div#{conversation_div_id}")
            if len(conversation_info_datas) > 0:
                try:
                    conversation_info = json.loads(
                        remove_json_invalid_control_chars(conversation_info_datas[0].inner_text()))
                    break
                except json.JSONDecodeError as e:
                    print("load conversation info JSONDecodeError:", e)
            sleep(0.2)
        self.page.evaluate(f"document.getElementById('{conversation_div_id}').remove()")

        return conversation_info

    def _set_logging(self, debug_log: Optional[str]) -> logging.Logger:

        logger = logging.getLogger(self.__class__.__name__)
        logger.setLevel(logging.DEBUG)
        log_console_handler = logging.StreamHandler()
        log_console_handler.setFormatter(log_formatter)
        log_console_handler.setLevel(logging.WARNING)
        logger.addHandler(log_console_handler)
        if debug_log:
            log_file_handler = logging.FileHandler(debug_log)
            log_file_handler.setFormatter(log_formatter)
            logger.addHandler(log_file_handler)
        return logger

    def _start_browser(self):
        self.page.goto("https://chat.openai.com/")

    def _cleanup(self):
        self.browser.close()
        # remove the user data dir in case this is a second instance
        if hasattr(self, "user_data_dir"):
            shutil.rmtree(self.user_data_dir)
        self.play.stop()

    def refresh_session(self):
        self.page.evaluate("""
        const xhr = new XMLHttpRequest();
        xhr.open('GET', 'https://chat.openai.com/api/auth/session');
        xhr.onload = () => {
          if(xhr.status == 200) {
            var mydiv = document.createElement('DIV');
            mydiv.id = "SESSION_DIV_ID"
            mydiv.innerHTML = xhr.responseText;
            document.body.appendChild(mydiv);
          }
        };
        xhr.send();
        """.replace("SESSION_DIV_ID", self.session_div_id))

        while True:
            session_datas = self.page.query_selector_all(f"div#{self.session_div_id}")
            if len(session_datas) > 0:
                break
            sleep(0.2)

        session_data = json.loads(session_datas[0].inner_text())
        self.session = session_data

        self.page.evaluate(f"document.getElementById('{self.session_div_id}').remove()")

    def _cleanup_divs(self):
        self.page.evaluate(f"document.getElementById('{self.stream_div_id}').remove()")
        self.page.evaluate(f"document.getElementById('{self.eof_div_id}').remove()")

    def _api_request_build_headers(self, custom_headers: dict[str, str] = {}) -> dict[str, str]:
        headers = {
            "Authorization": "Bearer %s" % self.session["accessToken"],
        }
        headers.update(custom_headers)
        return headers

    def _process_api_response(self, url: str, response: APIResponse | Any, method: str = "GET"):
        self.log.debug(f"{method} {url} response, OK: {response.ok}, TEXT: {response.text()}")
        json = None
        if response.ok:
            try:
                json: Any = response.json()
            except JSONDecodeError:
                pass
        if not response.ok or not json:
            self.log.debug(f"{response.status} {response.status_text} {response.headers}")
        return response.ok, json, response

    def _api_get_request(self,
                         url: str,
                         query_params: dict[str, Any] = {},
                         custom_headers: dict[str, Any] = {}):
        headers = self._api_request_build_headers(custom_headers)
        response = self.page.request.get(url, headers=headers, params=query_params)
        return self._process_api_response(url, response)

    def _api_post_request(self, url: str, data: dict[str, Any] = {}, custom_headers: dict[str, Any] = {}):
        headers = self._api_request_build_headers(custom_headers)
        response = self.page.request.post(url, headers=headers, data=data)
        return self._process_api_response(url, response, method="POST")

    def _api_patch_request(self, url: str, data: dict[str, Any] = {}, custom_headers: dict[str, Any] = {}):
        headers = self._api_request_build_headers(custom_headers)
        response = self.page.request.patch(url, headers=headers, data=data)
        return self._process_api_response(url, response, method="PATCH")

    def _set_title(self):
        if not self.conversation_id or self.conversation_id and self.conversation_title_set:
            return
        url = f"https://chat.openai.com/backend-api/conversation/gen_title/{self.conversation_id}"
        data = {
            "message_id": self.parent_message_id,
            "model": RENDER_MODELS[self.model],
        }
        ok, _json, _response = self._api_post_request(url, data)
        if ok:
            # TODO: Do we want to do anything with the title we got back?
            # response_data = response.json()
            self.conversation_title_set = True
        else:
            self.log.warning("Failed to set title")

    def delete_conversation(self, uuid: Optional[str] = None):
        if not self.session:
            self.refresh_session()
        if not uuid and not self.conversation_id:
            return
        id = uuid if uuid else self.conversation_id
        url = f"https://chat.openai.com/backend-api/conversation/{id}"
        data = {
            "is_visible": False,
        }
        ok, json, _response = self._api_patch_request(url, data)
        if ok:
            return json
        else:
            self.log.warning("Failed to delete conversation")

    def get_history(self, limit: int = 20, offset: int = 0) -> Any:
        if not self.session:
            self.refresh_session()
        url = "https://chat.openai.com/backend-api/conversations"
        query_params = {
            "offset": offset,
            "limit": limit,
        }
        ok, json, _response = self._api_get_request(url, query_params)
        if ok:
            history: dict[str, Any] = {}
            for item in json["items"]:
                history[item["id"]] = item
            return history
        else:
            self.log.warning("Failed to get history")

    def ask_stream(self, prompt: str):
        if not self.session:
            self.refresh_session()

        new_message_id = str(uuid.uuid4())

        if "accessToken" not in self.session:
            yield ("Your ChatGPT session is not usable.\n"
                   "* Run this program with the `install` parameter and log in to ChatGPT.\n"
                   "* If you think you are already logged in, try running the `session` command.")
            return

        request = {
            "messages": [{
                "id": new_message_id,
                "role": "user",
                "content": {
                    "content_type": "text",
                    "parts": [prompt]
                },
            }],
            "model": RENDER_MODELS[self.model],
            "conversation_id": self.conversation_id,
            "parent_message_id": self.parent_message_id,
            "action": "next",
        }

        code = ("""
            const stream_div = document.createElement('DIV');
            stream_div.id = "STREAM_DIV_ID";
            document.body.appendChild(stream_div);
            const xhr = new XMLHttpRequest();
            xhr.open('POST', 'https://chat.openai.com/backend-api/conversation');
            xhr.setRequestHeader('Accept', 'text/event-stream');
            xhr.setRequestHeader('Content-Type', 'application/json');
            xhr.setRequestHeader('Authorization', 'Bearer BEARER_TOKEN');
            xhr.responseType = 'stream';
            xhr.onreadystatechange = function() {
              var newEvent;
              if(xhr.readyState == 3 || xhr.readyState == 4) {
                const newData = xhr.response.substr(xhr.seenBytes);
                try {
                  const newEvents = newData.split(/\\n\\n/).reverse();
                  newEvents.shift();
                  if(newEvents[0] == "data: [DONE]") {
                    newEvents.shift();
                  }
                  if(newEvents.length > 0) {
                    newEvent = newEvents[0].substring(6);
                    // using XHR for eventstream sucks and occasionally ive seen incomplete
                    // json objects come through  JSON.parse will throw if that happens, and
                    // that should just skip until we get a full response.
                    JSON.parse(newEvent);
                  }
                } catch (err) {
                  console.log(err);
                  newEvent = undefined;
                }
                if(newEvent !== undefined) {
                  stream_div.innerHTML = btoa(newEvent);
                  xhr.seenBytes = xhr.responseText.length;
                }
              }
              if(xhr.readyState == 4) {
                const eof_div = document.createElement('DIV');
                eof_div.id = "EOF_DIV_ID";
                document.body.appendChild(eof_div);
              }
            };
            xhr.send(JSON.stringify(REQUEST_JSON));
            """.replace("BEARER_TOKEN",
                        self.session["accessToken"]).replace("REQUEST_JSON", json.dumps(request)).replace(
                            "STREAM_DIV_ID", self.stream_div_id).replace("EOF_DIV_ID", self.eof_div_id))

        self.page.evaluate(code)

        last_event_msg = ""
        start_time = time.time()
        while True:
            eof_datas = self.page.query_selector_all(f"div#{self.eof_div_id}")

            conversation_datas = self.page.query_selector_all(f"div#{self.stream_div_id}")
            if len(conversation_datas) == 0:
                continue

            full_event_message = None

            try:
                event_raw: bytes = base64.b64decode(conversation_datas[0].inner_html())
                if len(event_raw) > 0:
                    event = json.loads(event_raw)
                    if event is not None:
                        self.parent_message_id = event["message"]["id"]
                        self.conversation_id = event["conversation_id"]
                        full_event_message = "\n".join(event["message"]["content"]["parts"])
            except Exception:
                yield ("Failed to read response from ChatGPT.  Tips:\n"
                       " * Try again.  ChatGPT can be flaky.\n"
                       " * Use the `session` command to refresh your session, and then try again.\n"
                       " * Restart the program in the `install` mode and make sure you are logged in.")
                break

            if full_event_message is not None:
                chunk = full_event_message[len(last_event_msg):]
                last_event_msg = full_event_message
                yield chunk

            # if we saw the eof signal, this was the last event we
            # should process and we are done
            if len(eof_datas) > 0 or (((time.time() - start_time) > self.timeout) and
                                      full_event_message is None):
                break

            sleep(0.2)

        self._cleanup_divs()
        self._set_title()

    def ask(self, message: str) -> str:
        """
        Send a message to chatGPT and return the response.

        Args:
            message (str): The message to send.

        Returns:
            str: The response received from OpenAI.
        """
        response = list(self.ask_stream(message))
        return (
            reduce(operator.add, response) if len(response) > 0 else
            "Unusable response produced, maybe login session expired. Try 'pkill firefox' and 'chatgpt install'"
        )

    def new_conversation(self):
        self.parent_message_id = str(uuid.uuid4())
        self.conversation_id = None
        self.conversation_title_set = None
