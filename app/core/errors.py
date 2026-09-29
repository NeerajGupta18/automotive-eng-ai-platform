"""Domain errors, mapped to HTTP status codes in the API layer."""


class NotFound(Exception):      # 404
    pass


class Invalid(Exception):       # 422 - the request content is not acceptable
    pass


class Conflict(Exception):      # 409 - request is valid but the workflow state forbids it
    pass
