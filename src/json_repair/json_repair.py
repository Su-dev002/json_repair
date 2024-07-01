"""
This module will parse the JSON file following the BNF definition:

    <json> ::= <container>

    <primitive> ::= <number> | <string> | <boolean>
    ; Where:
    ; <number> is a valid real number expressed in one of a number of given formats
    ; <string> is a string of valid characters enclosed in quotes
    ; <boolean> is one of the literal strings 'true', 'false', or 'null' (unquoted)

    <container> ::= <object> | <array>
    <array> ::= '[' [ <json> *(', ' <json>) ] ']' ; A sequence of JSON values separated by commas
    <object> ::= '{' [ <member> *(', ' <member>) ] '}' ; A sequence of 'members'
    <member> ::= <string> ': ' <json> ; A pair consisting of a name, and a JSON value

If something is wrong (a missing parantheses or quotes for example) it will use a few simple heuristics to fix the JSON string:
- Add the missing parentheses if the parser believes that the array or object should be closed
- Quote strings or add missing single quotes
- Adjust whitespaces and remove line breaks

All supported use cases are in the unit tests
"""

import os
import json
from typing import Any, Dict, List, Optional, Union, TextIO, Tuple
import re


class StringFileWrapper:
    # This is a trick to simplify the code, transform the filedescriptor handling into a string handling
    def __init__(self, fd: TextIO) -> None:
        self.fd = fd
        self.length: int = 0

    def __getitem__(self, index: int) -> str:
        if isinstance(index, slice):
            self.fd.seek(index.start)
            value = self.fd.read(index.stop - index.start)
            self.fd.seek(index.start)
            return value
        else:
            self.fd.seek(index)
            return self.fd.read(1)

    def __len__(self) -> int:
        if self.length < 1:
            current_position = self.fd.tell()
            self.fd.seek(0, os.SEEK_END)
            self.length = self.fd.tell()
            self.fd.seek(current_position)
        return self.length

    def __setitem__(self) -> None:
        raise Exception("This is read-only!")


class LoggerConfig:
    # This is a type class to simplify the declaration
    def __init__(self, log_level: Optional[str]):
        self.log: List[Dict[str, str]] = []
        self.window: int = 10
        self.log_level: str = log_level if log_level else "none"


JSONReturnType = Union[Dict[str, Any], List[Any], str, float, int, bool, None]
DefinitionType = Dict[str, Union[Dict, List, str] ]

class JSONParser:
    def __init__(
        self,
        json_str: Union[str, StringFileWrapper],
        json_fd: Optional[TextIO],
        logging: Optional[bool],
    ) -> None:
        # The string to parse
        self.json_str = json_str
        # Alternatively, the file description with a json file in it
        if json_fd:
            # This is a trick we do to treat the file wrapper as an array
            self.json_str = StringFileWrapper(json_fd)
        # Index is our iterator that will keep track of which character we are looking at right now
        self.index: int = 0
        # This is used in the object member parsing to manage the special cases of missing quotes in key or value
        self.context: list[str] = []
        # Use this to log the activity, but only if logging is active
        self.logger = LoggerConfig(log_level="info" if logging else None)

    def parse(
        self,
    ) -> Union[JSONReturnType, Tuple[JSONReturnType, List[Dict[str, str]]]]:
        json = self.parse_json()
        if self.index < len(self.json_str):
            self.log(
                "The parser returned early, checking if there's more json elements",
                "info",
            )
            json = [json]
            last_index = self.index
            while self.index < len(self.json_str):
                j = self.parse_json()
                if j != "":
                    json.append(j)
                if self.index == last_index:
                    self.index += 1
                last_index = self.index
            # If nothing extra was found, don't return an array
            if len(json) == 1:
                self.log(
                    "There were no more elements, returning the element without the array",
                    "info",
                )
                json = json[0]
        if self.logger.log_level == "none":
            return json
        else:
            return json, self.logger.log

    def parse_json(
        self,
    ) -> JSONReturnType:
        while True:
            char = self.get_char_at()
            # This parser will ignore any basic element (string or number) that is not inside an array or object
            is_in_context = len(self.context) > 0
            # False means that we are at the end of the string provided
            if char is False:
                return ""
            # <object> starts with '{'
            elif char == "{":
                self.index += 1
                return self.parse_object()
            # <array> starts with '['
            elif char == "[":
                self.index += 1
                return self.parse_array()
            # there can be an edge case in which a key is empty and at the end of an object
            # like "key": }. We return an empty string here to close the object properly
            elif char == "}":
                self.log(
                    "At the end of an object we found a key with missing value, skipping",
                    "info",
                )
                return ""
            # <string> starts with a quote
            elif is_in_context and (char in ['"', "'", "“"] or char.isalpha()):
                return self.parse_string()
            # <number> starts with [0-9] or minus
            elif is_in_context and (char.isdigit() or char == "-" or char == "."):
                return self.parse_number()
            # If everything else fails, we just ignore and move on
            else:
                self.index += 1

    def parse_object(self) -> Dict[str, Any]:
        # <object> ::= '{' [ <member> *(', ' <member>) ] '}' ; A sequence of 'members'
        obj = {}
        # Stop when you either find the closing parentheses or you have iterated over the entire string
        while (self.get_char_at() or "}") != "}":
            # This is what we expect to find:
            # <member> ::= <string> ': ' <json>

            # Skip filler whitespaces
            self.skip_whitespaces_at()

            # Sometimes LLMs do weird things, if we find a ":" so early, we'll change it to "," and move on
            if (self.get_char_at() or "") == ":":
                self.log(
                    "While parsing an object we found a : before a key, ignoring",
                    "info",
                )
                self.index += 1

            # We are now searching for they string key
            # Context is used in the string parser to manage the lack of quotes
            self.set_context("object_key")

            self.skip_whitespaces_at()

            # <member> starts with a <string>
            key = ""
            while key == "" and self.get_char_at():
                current_index = self.index
                key = self.parse_string()

                # This can happen sometimes like { "": "value" }
                if key == "" and self.get_char_at() == ":":
                    key = "empty_placeholder"
                    self.log(
                        "While parsing an object we found an empty key, replacing with empty_placeholder",
                        "info",
                    )
                    break
                elif key == "" and self.index == current_index:
                    # Sometimes the string search might not move the index at all, that might lead us to an infinite loop
                    self.index += 1

            self.skip_whitespaces_at()

            # We reached the end here
            if (self.get_char_at() or "}") == "}":
                continue

            self.skip_whitespaces_at()

            # An extreme case of missing ":" after a key
            if (self.get_char_at() or "") != ":":
                self.log(
                    "While parsing an object we missed a : after a key",
                    "info",
                )

            self.index += 1
            self.reset_context()
            self.set_context("object_value")
            # The value can be any valid json
            value = self.parse_json()

            # Reset context since our job is done
            self.reset_context()
            obj[key] = value

            if (self.get_char_at() or "") in [",", "'", '"']:
                self.index += 1

            # Remove trailing spaces
            self.skip_whitespaces_at()

        # Especially at the end of an LLM generated json you might miss the last "}"
        if (self.get_char_at() or "}") != "}":
            self.log(
                "While parsing an object, we couldn't find the closing }, ignoring",
                "info",
            )

        self.index += 1
        return obj

    def parse_array(self) -> List[Any]:
        # <array> ::= '[' [ <json> *(', ' <json>) ] ']' ; A sequence of JSON values separated by commas
        arr = []
        self.set_context("array")
        # Stop when you either find the closing parentheses or you have iterated over the entire string
        while (self.get_char_at() or "]") != "]":
            self.skip_whitespaces_at()
            value = self.parse_json()

            # It is possible that parse_json() returns nothing valid, so we stop
            if value == "":
                break

            if value == "..." and self.get_char_at(-1) == ".":
                self.log(
                    "While parsing an array, found a stray '...'; ignoring it", "info"
                )
            else:
                arr.append(value)

            # skip over whitespace after a value but before closing ]
            char = self.get_char_at()
            while char and (char.isspace() or char == ","):
                self.index += 1
                char = self.get_char_at()
            # If this is the right value of an object and we are closing the object, it means the array is over
            if self.get_context() == "object_value" and char == "}":
                self.log(
                    "While parsing an array inside an object, we got to the end without finding a ]. Stopped parsing",
                    "info",
                )
                break

        # Especially at the end of an LLM generated json you might miss the last "]"
        char = self.get_char_at()
        if char and char != "]":
            self.log(
                "While parsing an array we missed the closing ], adding it back", "info"
            )
            # Sometimes when you fix a missing "]" you'll have a trailing "," there that makes the JSON invalid
            if char == ",":
                # Remove trailing "," before adding the "]"
                self.log(
                    "While parsing an array, found a trailing , before adding ]",
                    "info",
                )

            self.index -= 1

        self.index += 1
        self.reset_context()
        return arr

    def parse_string(self) -> Union[str, JSONReturnType]:
        # <string> is a string of valid characters enclosed in quotes
        # i.e. { name: "John" }
        # Somehow all weird cases in an invalid JSON happen to be resolved in this function, so be careful here

        # Flag to manage corner cases related to missing starting quote
        missing_quotes = False
        doubled_quotes = False
        lstring_delimiter = rstring_delimiter = '"'

        char = self.get_char_at()
        # A valid string can only start with a valid quote or, in our case, with a literal
        while char and char not in ['"', "'", "“"] and not char.isalpha():
            self.index += 1
            char = self.get_char_at()

        if not char:
            # This is an empty string
            return ""

        # Ensuring we use the right delimiter
        if char == "'":
            lstring_delimiter = rstring_delimiter = "'"
        elif char == "“":
            lstring_delimiter = "“"
            rstring_delimiter = "”"
        elif char.isalpha():
            # This could be a <boolean> and not a string. Because (T)rue or (F)alse or (N)ull are valid
            # But remember, object keys are only of type string
            if char.lower() in ["t", "f", "n"] and self.get_context() != "object_key":
                value = self.parse_boolean_or_null()
                if value != "":
                    return value
            self.log(
                "While parsing a string, we found a literal instead of a quote",
                "info",
            )
            self.log(
                "While parsing a string, we found no starting quote. Will add the quote back",
                "info",
            )
            missing_quotes = True

        if not missing_quotes:
            self.index += 1

        # There is sometimes a weird case of doubled quotes, we manage this also later in the while loop
        if self.get_char_at() == lstring_delimiter:
            # This is a valid exception only if it's closed by a double delimiter again
            i = 1
            next_c = self.get_char_at(i)
            while next_c and next_c != rstring_delimiter:
                i += 1
                next_c = self.get_char_at(i)
            # Now check that the next character is also a delimiter to ensure that we have "".....""
            # In that case we ignore this rstring delimiter
            if next_c and (self.get_char_at(i + 1) or "") == rstring_delimiter:
                self.log(
                    "While parsing a string, we found a valid starting doubled quote, ignoring it",
                    "info",
                )
                doubled_quotes = True
                self.index += 1

        # Initialize our return value
        string_acc = ""

        # Here things get a bit hairy because a string missing the final quote can also be a key or a value in an object
        # In that case we need to use the ":|,|}" characters as terminators of the string
        # So this will stop if:
        # * It finds a closing quote
        # * It iterated over the entire sequence
        # * If we are fixing missing quotes in an object, when it finds the special terminators
        char = self.get_char_at()
        while char and char != rstring_delimiter:
            if missing_quotes:
                if self.get_context() == "object_key" and (
                    char == ":" or char.isspace()
                ):
                    self.log(
                        "While parsing a string missing the left delimiter in object key context, we found a :, stopping here",
                        "info",
                    )
                    break
                elif self.get_context() == "object_value" and char in [",", "}"]:
                    rstring_delimiter_missing = True
                    # check if this is a case in which the closing comma is NOT missing instead
                    i = 1
                    next_c = self.get_char_at(i)
                    while next_c and next_c != rstring_delimiter:
                        i += 1
                        next_c = self.get_char_at(i)
                    if next_c:
                        i += 1
                        next_c = self.get_char_at(i)
                        # found a delimiter, now we need to check that is followed strictly by a comma or brace
                        while next_c and next_c.isspace():
                            i += 1
                            next_c = self.get_char_at(i)
                        if next_c and next_c in [",", "}"]:
                            rstring_delimiter_missing = False
                    if rstring_delimiter_missing:
                        self.log(
                            "While parsing a string missing the left delimiter in object value context, we found a , or } and we couldn't determine that a right delimiter was present. Stopping here",
                            "info",
                        )
                        break
            string_acc += char
            self.index += 1
            char = self.get_char_at()
            if len(string_acc) > 1 and string_acc[-1] == "\\":
                # This is a special case, if people use real strings this might happen
                self.log("Found a stray escape sequence, normalizing it", "info")
                string_acc = string_acc[:-1]
                if char in [rstring_delimiter, "t", "n", "r", "b", "\\"]:
                    escape_seqs = {"t": "\t", "n": "\n", "r": "\r", "b": "\b"}
                    string_acc += escape_seqs.get(char, char) or char
                    self.index += 1
                    char = self.get_char_at()
            # ChatGPT sometimes forget to quote stuff in html tags or markdown, so we do this whole thing here
            if char == rstring_delimiter:
                # Special case here, in case of double quotes one after another
                if doubled_quotes and self.get_char_at(1) == rstring_delimiter:
                    self.log(
                        "While parsing a string, we found a doubled quote, ignoring it",
                        "info",
                    )
                elif missing_quotes and self.get_context() == "object_value":
                    # In case of missing starting quote I need to check if the delimeter is the end or the beginning of a key
                    i = 1
                    next_c = self.get_char_at(i)
                    while next_c and next_c not in [
                        rstring_delimiter,
                        lstring_delimiter,
                    ]:
                        i += 1
                        next_c = self.get_char_at(i)
                    if next_c:
                        # We found a quote, now let's make sure there's a ":" following
                        i += 1
                        next_c = self.get_char_at(i)
                        # found a delimiter, now we need to check that is followed strictly by a comma or brace
                        while next_c and next_c.isspace():
                            i += 1
                            next_c = self.get_char_at(i)
                        if next_c and next_c == ":":
                            # Reset the cursor
                            self.index -= 1
                            char = self.get_char_at()
                            self.log(
                                "In a string with missing quotes and object value context, I found a delimeter but it turns out it was the beginning on the next key. Stopping here.",
                                "info",
                            )
                            break
                else:
                    # Check if eventually there is a rstring delimiter, otherwise we bail
                    i = 1
                    next_c = self.get_char_at(i)
                    check_comma_in_object_value = True
                    while next_c and next_c not in [
                        rstring_delimiter,
                        lstring_delimiter,
                    ]:
                        # This is a bit of a weird workaround, essentially in object_value context we don't always break on commas
                        # This is because the routine after will make sure to correct any bad guess and this solves a corner case
                        if next_c.isalpha():
                            check_comma_in_object_value = False
                        # If we are in an object context, let's check for the right delimiters
                        if (
                            ("object_key" in self.context and next_c in [":", "}"])
                            or ("object_value" in self.context and next_c == "}")
                            or ("array" in self.context and next_c in ["]", ","])
                            or (
                                check_comma_in_object_value
                                and self.get_context() == "object_value"
                                and next_c == ","
                            )
                        ):
                            break
                        i += 1
                        next_c = self.get_char_at(i)
                    # If we stopped for a comma in object_value context, let's check if find a "} at the end of the string
                    if next_c == "," and self.get_context() == "object_value":
                        i += 1
                        next_c = self.get_char_at(i)
                        while next_c and next_c != rstring_delimiter:
                            i += 1
                            next_c = self.get_char_at(i)
                        # Ok now I found a delimiter, let's skip whitespaces and see if next we find a }
                        i += 1
                        next_c = self.get_char_at(i)
                        while next_c and next_c.isspace():
                            i += 1
                            next_c = self.get_char_at(i)
                        if next_c == "}":
                            # OK this is valid then
                            self.log(
                                "While parsing a string, we a misplaced quote that would have closed the string but has a different meaning here since this is the last element of the object, ignoring it",
                                "info",
                            )
                            string_acc += char
                            self.index += 1
                            char = self.get_char_at()
                    elif next_c == rstring_delimiter:
                        if self.get_context() == "object_value":
                            # But this might not be it! This could be just a missing comma
                            # We found a delimiter and we need to check if this is a key
                            # so find a rstring_delimiter and a colon after
                            i += 1
                            next_c = self.get_char_at(i)
                            while next_c and next_c != rstring_delimiter:
                                i += 1
                                next_c = self.get_char_at(i)
                            i += 1
                            next_c = self.get_char_at(i)
                            while next_c and next_c != ":":
                                if next_c in [
                                    lstring_delimiter,
                                    rstring_delimiter,
                                    ",",
                                ]:
                                    break
                                i += 1
                                next_c = self.get_char_at(i)
                            # Only if we fail to find a ':' then we know this is misplaced quote
                            if next_c != ":":
                                self.log(
                                    "While parsing a string, we a misplaced quote that would have closed the string but has a different meaning here, ignoring it",
                                    "info",
                                )
                                string_acc += char
                                self.index += 1
                                char = self.get_char_at()

        if (
            char
            and missing_quotes
            and self.get_context() == "object_key"
            and char.isspace()
        ):
            self.log(
                "While parsing a string, handling an extreme corner case in which the LLM added a comment instead of valid string, invalidate the string and return an empty value",
                "info",
            )
            self.skip_whitespaces_at()
            if self.get_char_at() not in [":", ","]:
                return ""

        # A fallout of the previous special case in the while loop, we need to update the index only if we had a closing quote
        if char != rstring_delimiter:
            self.log(
                "While parsing a string, we missed the closing quote, ignoring",
                "info",
            )
        else:
            self.index += 1

        return string_acc.rstrip()

    def parse_number(self) -> Union[float, int, str, JSONReturnType]:
        # <number> is a valid real number expressed in one of a number of given formats
        number_str = ""
        number_chars = set("0123456789-.eE/,")
        char = self.get_char_at()
        is_array = self.get_context() == "array"
        while char and char in number_chars and (char != "," or not is_array):
            number_str += char
            self.index += 1
            char = self.get_char_at()
        if len(number_str) > 1 and number_str[-1] in "-eE/,":
            # The number ends with a non valid character for a number/currency, rolling back one
            number_str = number_str[:-1]
            self.index -= 1
        if number_str:
            try:
                if "," in number_str:
                    return str(number_str)
                if "." in number_str or "e" in number_str or "E" in number_str:
                    return float(number_str)
                elif number_str == "-":
                    # If there is a stray "-" this will throw an exception, throw away this character
                    return self.parse_json()
                else:
                    return int(number_str)
            except ValueError:
                return number_str
        else:
            # If nothing works, let's skip and keep parsing
            return self.parse_json()

    def parse_boolean_or_null(self) -> Union[bool, str, None]:
        # <boolean> is one of the literal strings 'true', 'false', or 'null' (unquoted)
        starting_index = self.index
        char = (self.get_char_at() or "").lower()
        if char == "t":
            value = ("true", True)
        elif char == "f":
            value = ("false", False)
        elif char == "n":
            value = ("null", None)

        if value:
            i = 0
            while char and i < len(value[0]) and char == value[0][i]:
                i += 1
                self.index += 1
                char = (self.get_char_at() or "").lower()
            if i == len(value[0]):
                return value[1]

        # If nothing works reset the index before returning
        self.index = starting_index
        return ""

    def get_char_at(self, count: int = 0) -> Union[str, bool]:
        # Why not use something simpler? Because try/except in python is a faster alternative to an "if" statement that is often True
        try:
            return self.json_str[self.index + count]
        except IndexError:
            return False

    def skip_whitespaces_at(self) -> None:
        """
        This function quickly iterates on whitespaces, syntactic sugar to make the code more concise
        """
        try:
            char = self.json_str[self.index]
        except IndexError:
            return
        while char.isspace():
            self.index += 1
            try:
                char = self.json_str[self.index]
            except IndexError:
                return

    def set_context(self, value: str) -> None:
        # If a value is provided update the context variable and save in stack
        if value:
            self.context.append(value)

    def reset_context(self) -> None:
        try:
            self.context.pop()
        except Exception:
            return

    def get_context(self) -> str:
        try:
            return self.context[-1]
        except Exception:
            return ""

    def log(self, text: str, level: str) -> None:
        if level == self.logger.log_level:
            context = ""
            start = max(self.index - self.logger.window, 0)
            end = min(self.index + self.logger.window, len(self.json_str))
            context = self.json_str[start:end]
            self.logger.log.append(
                {
                    "text": text,
                    "context": context,
                }
            )


def repair_json(
    json_str: str = "",
    return_objects: Optional[bool] = False,
    skip_json_loads: Optional[bool] = False,
    logging: Optional[bool] = False,
    json_fd: Optional[TextIO] = None,
) -> Union[JSONReturnType, Tuple[JSONReturnType, List[Dict[str, str]]]]:
    """
    Given a json formatted string, it will try to decode it and, if it fails, it will try to fix it.
    It will return the fixed string by default.
    When `return_objects=True` is passed, it will return the decoded data structure instead.
    When `skip_json_loads=True` is passed, it will not call the built-in json.loads() function
    When `logging=True` is passed, it will return an tuple with the repaired json and a log of all repair actions
    """
    parser = JSONParser(json_str, json_fd, logging)
    if skip_json_loads:
        parsed_json = parser.parse()
    else:
        try:
            if json_fd:
                parsed_json = json.load(json_fd)
            else:
                parsed_json = json.loads(json_str)
        except json.JSONDecodeError:
            parsed_json = parser.parse()
    # It's useful to return the actual object instead of the json string, it allows this lib to be a replacement of the json library
    if return_objects or logging:
        return parsed_json
    return json.dumps(parsed_json)


def loads(
    json_str: str, skip_json_loads: bool = False, logging: bool = False
) -> Union[JSONReturnType, Tuple[JSONReturnType, List[Dict[str, str]]]]:
    """
    This function works like `json.loads()` except that it will fix your JSON in the process.
    It is a wrapper around the `repair_json()` function with `return_objects=True`.
    """
    return repair_json(
        json_str=json_str,
        return_objects=True,
        skip_json_loads=skip_json_loads,
        logging=logging,
    )


def load(
    fd: TextIO, skip_json_loads: bool = False, logging: bool = False
) -> Union[JSONReturnType, Tuple[JSONReturnType, List[Dict[str, str]]]]:
    """
    This function works like `json.load()` except that it will fix your JSON in the process.
    It is a wrapper around the `repair_json()` function with `json_fd=fd` and `return_objects=True`.
    """
    return repair_json(json_fd=fd, skip_json_loads=skip_json_loads, logging=logging)


def from_file(
    filename: str, skip_json_loads: bool = False, logging: bool = False
) -> Union[JSONReturnType, Tuple[JSONReturnType, List[Dict[str, str]]]]:
    """
    This function is a wrapper around `load()` so you can pass the filename as string
    """
    fd = open(filename)
    jsonobj = load(fd, skip_json_loads, logging)
    fd.close()

    return jsonobj


def replace_single_quotes(input_str: str):
    """
    Replace single quotes separated by any number of spaces with double quotes separated by space.

    Args:
        input_str (str): The input string to replace quotes in.

    Returns:
        str: The modified string with double quotes.
    """

    output_str = re.sub(r"'\s+'", '" "', input_str)
    return output_str


def empty_val(prop_def: DefinitionType) -> Any:
  """
    Takes a JSON definition and returns an empty object according to the definition type.

    Args:
        prop_def (DefinitionType): The JSON definition.

    Returns:
        Any: An empty object of the type specified in the definition.
    """

  if prop_def.get('type') == 'string':
    return ''
  elif prop_def.get('type') == 'integer':
    return 0
  elif prop_def.get('type') == 'array':
    return []
  elif prop_def.get('type') == 'object':
    return {}
  else:
    return None


def correct_int(value: Any) -> int:
    """
    Try to convert other types into an integer if possible.

    Args:
        value (Any): The value to convert.

    Returns:
        int: The converted integer value, or None if conversion is not possible.
    """
    if isinstance(value, str) and value.isdigit():
            return int(value)
    elif isinstance(value, float):
        return int(value)  # or round(value)
    elif isinstance(value, bool):
        return int(value)
    # if we got a list instead of the int we check all items are of int or not 
    # if yes we keep the first int 
    elif isinstance(value, list) and all(isinstance(item, int) for item in value):
        return value[0]


def correct_str(value : Any, is_enum : bool = False) -> str:
    """
    Try to convert other types into a string if possible.

    Args:
        value (Any): The value to convert.
        is_enum (bool, optional): Whether the value is an enumeration. Defaults to False.

    Returns:
        str: The converted string value, or None if conversion is not possible.
    """
    # if we got a list instead of the string we check all items are of string or not 
    # if yes we return all string concatenated 
    # if it was enum we return first string
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        if is_enum:
            return value[0]
        else:
            return ' '.join(value)
        
    elif isinstance(value, bool):
        return str(value)
    elif isinstance(value, int):
        return str(value)
    

def correct_list(value: Any, definition: DefinitionType) -> List:
    """
    Try to convert other types into a list if possible.

    Args:
        value (Any): The value to convert.
        definition (DefinitionType): The JSON definition of the list.

    Returns:
        List: The converted list value, or None if conversion is not possible.
    """
    
    item_type = definition['items']
    if isinstance(value, str):
        # if an list was expected but we got string, and the string had substrings with some delimiter, 
        # it can be any delimiters() , then we enclose that string in an list, repair it again
        # and check type of a new list again
        new_value = replace_single_quotes(value)
        corrected_value = repair_json(f"""["{new_value}"]""", return_objects=True)
        return check_type(corrected_value, definition)
    else:
        # if we got a object of any type other than list or string, 
        # check whether the type of object is same as the type required of items in list
        # if yes return that object eclosed in string 
        valid_item = check_type(value, item_type)
        if valid_item is not None:
            return [valid_item]


def correct_dict(value: Any, definition: DefinitionType) -> Dict:
    """
    Attempts to correct a value to a dictionary based on a definition.

    Args:
        value (Any): The value to correct.
        definition (DefinitionType): The definition to correct against.

    Returns:
        Dict: A dictionary if the value can be corrected, otherwise an empty dictionary.

    Notes:
        If the value is a list, the function will check if all items in the list are dictionaries.
        If so, it will return the first dictionary in the list, corrected against the definition.
        If the value is a string or an integer, the function will return an empty dictionary.
    """
    if isinstance(value, list) and all(isinstance(item, dict) for item in value):
            return check_type(value[0], definition)
    elif isinstance(value, str):
        return {}
    elif isinstance(value, int):
        return {}


def check_type(value: Any, definition: DefinitionType) -> Any:
    """
    Recursively validates a value against a definition and attempts to correct it if possible.

    Args:
        value (Any): The value to validate.
        definition (DefinitionType): The definition to validate against.

    Returns:
        Any: The validated and corrected value if it matches the definition, or None if it does not.

    Notes:
        If the value is of a different type but can be converted to the required type, the function will attempt to correct it.
        If the value is of a different type and cannot be corrected, it will be discarded from its parent object, if the parent object is list. if the parent object is dictionary,
        then parent object is discarded from its parent.
        If a dictionary is missing some keys, the function will add the keys with empty values.
        The function supports recursive validation of objects and arrays.
    """


    expected_type = definition.get('type')
    enum_values = definition.get('enum')


    # we only type check enum , do not check whether value was an valid option or not
    if enum_values:
        if expected_type == 'string':
            if isinstance(value, str):
                return value
            else:
                return correct_str(value, is_enum = True)

        elif expected_type == 'integer':
            if isinstance(value, int):
                # if value in enum_values:
                return value
            else:
                return correct_int(value)
        return None


    if expected_type == 'string':
        if isinstance(value, str):
            return value
        else:
            return correct_str(value)
        

    elif expected_type == 'integer':
        if isinstance(value, int):
            return value
        else:
            return correct_int(value)
        

    elif expected_type == 'array':
        item_type = definition['items']
        if isinstance(value, list):
            # only keep items which matched the definition or were corrected
            valid_items = [item for item in [check_type(i, item_type) for i in value] if item]
            return valid_items
        else:
            return correct_list(value, definition)
        

    elif expected_type == 'object':
        if isinstance(value, dict):
            properties = definition.get('properties', {})
            valid_object = {}
            for key, prop_def in properties.items():
                if key in value:
                    valid_value = check_type(value[key], prop_def)
                    # if any value of dictionary did follow the type or did not get corrected, return empty dictionary 
                    if valid_value is None:
                        # print(f"key:{key} does not have an valid value")
                        return {}
                    valid_object[key] = valid_value
                else:
                    #if any key was not found, we add that key with an empty object of expected type
                    # print(f"expected key: {key} not found")
                    valid_object[key] = empty_val(prop_def)
            return valid_object
        
        else:
            return correct_dict(value, definition)

    return None


def validate_json(json_str: str, definition: str = None ) -> Any:
    """
    Validates a JSON string against a given definition.

    Args:
        json_str (str): The JSON string to validate.
        definition (str, optional): The JSON schema definition to validate against. Defaults to None.

    Returns:
        Any: The validated JSON data if it matches the definition, or None if it does not.

    Notes:
        If no definition is provided, the function will simply return the repaired JSON data.
        If the definition is an object, the function will check that the JSON data matches the object's properties.
        If the definition is an array, the function will check that each item in the JSON data matches the array's item definition.
    """
    output = repair_json(json_str, return_objects=True)
    if definition:
        definition_json = json.loads(definition)
        parameters_def = definition_json[0]["parameters"]

        if parameters_def['type'] == 'object' and "properties" in parameters_def:
            return check_type(output, parameters_def)
        elif parameters_def['type'] == 'array':
            if not isinstance(output, list):
                return None
            valid_items = [check_type(item, parameters_def['items']) for item in output]
            return [item for item in valid_items if item is not None]
    else:
        return output
    return None
