#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright (C) 2022 Andy Stewart
#
# Author:     Andy Stewart <lazycat.manatee@gmail.com>
# Maintainer: Andy Stewart <lazycat.manatee@gmail.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
import pprint
import threading
import time
import os
from typing import Dict, Tuple, TYPE_CHECKING

from core.lspserver import LspServer
from core.utils import *
from core.handler import *

if TYPE_CHECKING:
    from lsp_bridge import LspBridge

def create_file_action_with_single_server(filepath, single_server_info, single_server, external_file_link=None):
    if is_in_path_dict(FILE_ACTION_DICT, filepath):
        if get_from_path_dict(FILE_ACTION_DICT, filepath).single_server != single_server:
            logger.warn("File {} is opened by different lsp server.".format(filepath))
        return
    
    action = FileAction(filepath, single_server_info, single_server, None, None, external_file_link)
    add_to_path_dict(FILE_ACTION_DICT, filepath, action)
    return action

def create_file_action_with_multi_servers(filepath, multi_servers_info, multi_servers, external_file_link=None):
    action = FileAction(filepath, None, None, multi_servers_info, multi_servers, external_file_link)
    add_to_path_dict(FILE_ACTION_DICT, filepath, action)
    return action

class FileAction:
    def __init__(self, filepath, single_server_info, single_server, multi_servers_info, multi_servers, external_file_link):
        # Init.
        self.single_server_info = single_server_info
        self.single_server: LspServer = single_server
        self.multi_servers = multi_servers
        self.multi_servers_info = multi_servers_info
        
        self.code_action_response = None
        self.completion_item_resolve_key = None
        self.completion_items = {}
        self.diagnostics = []
        self.diagnostics_ticker = 0
        self.external_file_link = external_file_link
        self.filepath = filepath
        self.last_change_cursor_time = -1.0
        self.last_change_file_time = -1.0
        self.request_dict = {}
        self.try_completion_timer = None
        self.version = 1

        # Initialize handlers.
        self.handlers: Dict[str, Handler] = dict()
        logger.debug("Handlers: " + pprint.pformat(Handler.__subclasses__()))
        for handler_cls in Handler.__subclasses__():
            self.handlers[handler_cls.name] = handler_cls(self)
            
        (self.enable_auto_import, 
         self.completion_items_limit, 
         self.insert_spaces,
         self.enable_push_diagnostics,
         self.push_diagnostic_idle,
         self.display_label_max_length) = get_emacs_vars([
             "acm-backend-lsp-enable-auto-import",
             "acm-backend-lsp-candidates-max-number",
             "indent-tabs-mode",
             "lsp-bridge-enable-diagnostics",
             "lsp-bridge-diagnostic-fetch-idle",
             "acm-backend-lsp-candidate-max-length"
        ])
        self.insert_spaces = not self.insert_spaces

        self.method_handlers = {}
        for lsp_server in self.get_lsp_servers():
            method_handlers_dict = {}
            for handler_cls in Handler.__subclasses__():
                method_handlers_dict[handler_cls.name] = handler_cls(self)
                    
            self.method_handlers[lsp_server.server_info["name"]] = method_handlers_dict
            
            lsp_server.attach(self)
            
        # Set acm-input-bound-style when opened file.
        if self.single_server_info != None:
            eval_in_emacs("lsp-bridge-set-prefix-style", self.single_server_info.get("prefixStyle", "ascii"))

    @property
    def last_change(self) -> Tuple[float, float]:
        """Return the last change information as a tuple."""
        return self.last_change_file_time, self.last_change_cursor_time
    
    def call(self, method, *args, **kwargs):
        """Call any handler or method of file action."""
        if method in self.handlers:
            handler = self.handlers[method]
            if self.single_server:
                self.send_request(self.single_server, method, handler, *args, **kwargs)
            else:
                if method in ["completion", "completion_item_resolve", "diagnostics", "code_action", "execute_command"]:
                    method_server_names = self.multi_servers_info[method]
                else:
                    method_server_names = [self.multi_servers_info[method]]
                    
                for method_server_name in method_server_names:
                    method_server = self.multi_servers[method_server_name]
                    self.send_request(method_server, method, handler, *args, **kwargs)
        elif hasattr(self, method):
            getattr(self, method)(*args, **kwargs)
            
    def send_request(self, method_server, method, handler, *args, **kwargs):
        if hasattr(handler, "provider"):
            if getattr(method_server, getattr(handler, "provider")):
                self.send_server_request(method_server, method, *args, **kwargs) 
            elif hasattr(handler, "provider_message"):
                message_emacs(getattr(handler, "provider_message"))
        else:
            self.send_server_request(method_server, method, *args, **kwargs) 
            
    def change_file(self, start, end, range_length, change_text, position, before_char, buffer_name, prefix):
        buffer_content = ''
        # Send didChange request to LSP server.
        for lsp_server in self.get_lsp_servers():
            if lsp_server.text_document_sync == 0:
                continue
            elif lsp_server.text_document_sync == 1:
                if not buffer_content:
                    buffer_content = get_emacs_func_result('get-buffer-content', buffer_name)
                lsp_server.send_whole_change_notification(self.filepath, self.version, buffer_content)
            else:
                lsp_server.send_did_change_notification(self.filepath, self.version, start, end, range_length, change_text)

        self.version += 1

        # Try cancel expired completion timer.
        if self.try_completion_timer is not None and self.try_completion_timer.is_alive():
            self.try_completion_timer.cancel()

        # Record last change information.
        self.last_change_file_time = time.time()

        # Send textDocument/completion 100ms later.
        self.try_completion_timer = threading.Timer(0.1, lambda : self.try_completion(position, before_char, prefix))
        self.try_completion_timer.start()
        
    def update_file(self, buffer_name):
        buffer_content = get_emacs_func_result('get-buffer-content', buffer_name)
        for lsp_server in self.get_lsp_servers():
            lsp_server.send_whole_change_notification(self.filepath, self.version, buffer_content)

        self.version += 1

    def try_completion(self, position, before_char, prefix):
        # Only send textDocument/completion request when match one of following rules:
        # 1. Character before cursor is match completion trigger characters.
        # 2. Completion UI is invisible.
        # 3. Last completion candidates is empty.
        if self.multi_servers:
            for lsp_server in self.multi_servers.values():
                if lsp_server.server_info["name"] in self.multi_servers_info["completion"]:
                    self.send_server_request(lsp_server, "completion", lsp_server, position, before_char, prefix)
        else:
            self.send_server_request(self.single_server, "completion", self.single_server, position, before_char, prefix)
                
    def change_cursor(self, position):
        # Record change cursor time.
        self.last_change_cursor_time = time.time()
        
    def ignore_diagnostic(self):
        lsp_server = self.get_match_lsp_servers("completion")[0]
        if "ignore-diagnostic" in lsp_server.server_info:
            eval_in_emacs("lsp-bridge-diagnostic--ignore", lsp_server.server_info["ignore-diagnostic"])
        else:
            message_emacs("Not found 'ignore_diagnostic' field in LSP server configure file.")
            
    def list_diagnostics(self):
        if len(self.diagnostics) == 0:
            message_emacs("No diagnostics found.")
        else:
            eval_in_emacs("lsp-bridge-diagnostic--list", self.diagnostics)
            
    def sort_diagnostic(self, diagnostic_a, diagnostic_b):
        score_a = [diagnostic_a["range"]["start"]["line"],
                   diagnostic_a["range"]["start"]["character"],
                   diagnostic_a["range"]["end"]["line"],
                   diagnostic_a["range"]["end"]["character"]]
        score_b = [diagnostic_b["range"]["start"]["line"],
                   diagnostic_b["range"]["start"]["character"],
                   diagnostic_b["range"]["end"]["line"],
                   diagnostic_b["range"]["end"]["character"]]
        
        if score_a < score_b:
            return -1
        elif score_a > score_b:
            return 1
        else:
            return 0
        
    def record_diagnostics(self, diagnostics):
        # Record diagnostics data that push from LSP server.
        import functools
        self.diagnostics = sorted(diagnostics, key=functools.cmp_to_key(self.sort_diagnostic))
        self.diagnostics_ticker += 1 
        
        # Try to push diagnostics to Emacs.
        if self.enable_push_diagnostics:
            push_diagnostic_ticker = self.diagnostics_ticker
            push_diagnostic_timer = threading.Timer(self.push_diagnostic_idle, lambda : self.try_push_diagnostics(push_diagnostic_ticker))
            push_diagnostic_timer.start()
        
    def try_push_diagnostics(self, ticker):
        # Only push diagnostics to Emacs when ticker is newest.
        # Drop all temporarily diagnostics when typing.
        if ticker == self.diagnostics_ticker:
            eval_in_emacs("lsp-bridge-diagnostic--render", self.filepath, self.diagnostics)
            
    def save_file(self, buffer_name):
        for lsp_server in self.get_lsp_servers():
            lsp_server.send_did_save_notification(self.filepath, buffer_name)
            
    def completion_item_resolve(self, item_key, server_name):
        if server_name in self.completion_items:
            if item_key in self.completion_items[server_name]:
                self.completion_item_resolve_key = item_key
                
                if self.multi_servers:
                    method_server = self.multi_servers[server_name]
                else:
                    method_server = self.single_server
                    
                if method_server.completion_resolve_provider:
                    self.send_server_request(method_server, "completion_item_resolve", item_key, server_name, self.completion_items[server_name][item_key])
                else:
                    item = self.completion_items[server_name][item_key]
                    
                    self.completion_item_update(
                        item_key, 
                        server_name,
                        item["documentation"] if "documentation" in item else "",
                        item["additionalTextEdits"] if "additionalTextEdits" in item else "")
                    
    def completion_item_update(self, item_key, server_name, documentation, additional_text_edits):
        if self.completion_item_resolve_key == item_key:
           if type(documentation) == dict:
               if "kind" in documentation:
                   documentation = documentation["value"]
                       
           eval_in_emacs("lsp-bridge-completion-item--update",
                         {
                             "filepath": self.filepath,
                             "key": item_key,
                             "server": server_name,
                             "additionalTextEdits": additional_text_edits,
                             "documentation": documentation
                         })


    def rename_file(self, old_filepath, new_filepath):
        self.get_lsp_servers()[0].send_did_rename_files_notification(old_filepath, new_filepath)

    def send_server_request(self, lsp_server, handler_name, *args, **kwargs):
        handler: Handler = self.method_handlers[lsp_server.server_info["name"]][handler_name]
        
        handler.latest_request_id = request_id = generate_request_id()
        handler.last_change = self.last_change

        lsp_server.record_request_id(request_id, handler)
        
        params = handler.process_request(*args, **kwargs)
        if handler.send_document_uri:
            params["textDocument"] = {"uri": lsp_server.parse_document_uri(self.filepath, self.external_file_link)}

        lsp_server.sender.send_request(
            method=handler.method,
            params=params,
            request_id=request_id)
        
    def exit(self):
        for lsp_server in self.get_lsp_servers():
            if lsp_server.server_name in LSP_SERVER_DICT:
                lsp_server = LSP_SERVER_DICT[lsp_server.server_name]
                lsp_server.close_file(self.filepath)
            
        # Clean FILE_ACTION_DICT after close file.
        remove_from_path_dict(FILE_ACTION_DICT, self.filepath)
        
    def get_lsp_servers(self):
        return self.multi_servers.values() if self.multi_servers else [self.single_server]
    
    def get_lsp_server_names(self):
        return list(map(lambda lsp_server: lsp_server.server_info["name"], self.get_lsp_servers()))
    
    def get_lsp_server_project_path(self):
        return self.single_server.project_path.encode('utf-8')
    
    def get_match_lsp_servers(self, method):
        if self.multi_servers:
            server_names = self.multi_servers_info[method]    # type: ignore
            if type(server_names) == str:
                server_names = [server_names]
                
            return list(map(lambda server_name: self.multi_servers[server_name], server_names))    # type: ignore
        else:
            return [self.single_server]
    
    def create_external_file_action(self, external_file, external_file_link=None):
        if self.multi_servers:
            create_file_action_with_multi_servers(external_file, self.multi_servers_info, self.multi_servers, external_file_link)
        else:
            create_file_action_with_single_server(external_file, self.single_server_info, self.single_server, external_file_link)
        
FILE_ACTION_DICT: Dict[str, FileAction] = {}  # use for contain file action
LSP_SERVER_DICT: Dict[str, LspServer] = {}  # use for contain lsp server

    
