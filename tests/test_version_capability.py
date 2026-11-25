import pytest

from azure_functions_langgraph._execution_plan import method_accepts_version


def test_method_accepts_explicit_version_param() -> None:
    # Given a callable with an explicit keyword-only version parameter.
    def fn(input: str, *, version: str | None = None) -> None: ...

    # When probing its version capability.
    accepts_version = method_accepts_version(fn)

    # Then version can be forwarded.
    assert accepts_version is True


def test_method_accepts_var_keyword() -> None:
    # Given a callable accepting arbitrary keyword arguments.
    def fn(input: str, **kwargs: str) -> None: ...

    # When probing its version capability.
    accepts_version = method_accepts_version(fn)

    # Then version can be forwarded through the catch-all.
    assert accepts_version is True


def test_method_without_version_rejected() -> None:
    # Given a callable with no version parameter or keyword catch-all.
    def fn(input: str, config: str | None = None) -> None: ...

    # When probing its version capability.
    accepts_version = method_accepts_version(fn)

    # Then version must not be forwarded.
    assert accepts_version is False


def test_builtin_without_version_rejected() -> None:
    # Given a builtin without a version parameter or keyword catch-all.
    method = print

    # When probing its version capability.
    accepts_version = method_accepts_version(method)

    # Then version must not be forwarded, whether its signature is inspectable or not.
    assert accepts_version is False


@pytest.mark.parametrize("error_type", (TypeError, ValueError))
def test_uninspectable_method_rejected(error_type: type[Exception]) -> None:
    # Given a callable whose signature cannot be inspected.
    class Opaque:
        @property
        def __signature__(self) -> None:
            raise error_type("no signature")

        def __call__(self, input: str) -> None: ...

    method = Opaque()

    # When probing its version capability.
    accepts_version = method_accepts_version(method)

    # Then failed introspection must not advertise version support.
    assert accepts_version is False
