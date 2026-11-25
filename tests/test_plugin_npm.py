"""Offline npm dependency resolution: required packages must exist at pinned versions."""

import json

import pytest

import test_plugin_feed
from agent_sessions.plugins import feed, npm_closure


@pytest.mark.parametrize(
    "version,spec,yes",
    [
        ("1.2.3", "^1.0.0", True),
        ("2.0.0", "^1.0.0", False),
        ("0.2.9", "^0.2.1", True),
        ("0.3.0", "^0.2.1", False),
        ("0.0.2", "^0.0.1", False),
        ("0.0.2", "^0.0", True),
        ("1.2.9", "~1.2.1", True),
        ("1.3.0", "~1.2.1", False),
        ("1.3.0", "~1", True),
        ("1.2.9", "1.2.x", True),
        ("1.3.0", "1.2", False),
        ("2.5.0", ">=1.0.0 <3.0.0", True),
        ("3.0.0", ">=1.0.0 <3.0.0", False),
        ("3.0.0", "1 || 3", True),
        ("2.3.9", "1.2 - 2.3", True),
        ("2.4.0", "1.2 - 2.3", False),
        ("1.2.3", "=1.2.3", True),
        ("1.2.3", "*", True),
        ("1.2.3-beta.1", "^1.0.0", False),
        ("1.2.3-beta.1", "1.2.3-beta.1", True),
        ("1.2.3+build.1", "1.2.3", True),
        ("1.2.3", ">1.2", False),
    ],
)
def test_closed_semver_ranges(version, spec, yes):
    assert npm_closure.satisfies(version, spec) is yes


@pytest.mark.parametrize("spec", ["latest", "file:../x", "https://evil.test/x", "workspace:*", ""])
def test_unpinned_or_non_registry_dependency_kinds_refuse(spec):
    with pytest.raises(feed.FeedError):
        npm_closure.satisfies("1.2.3", spec)


def package(root, destination, name, version, **fields):
    folder = root / destination
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "package.json").write_text(json.dumps({"name": name, "version": version, **fields}))


def test_missing_required_dependency_refuses_even_when_peer_meta_says_optional(tmp_path):
    entry = feed.entry(test_plugin_feed.entry(), signed=False)
    package(
        tmp_path,
        "node_modules/fixture",
        "fixture",
        "1.0.0",
        dependencies={"dep": "^1.0.0"},
        peerDependenciesMeta={"dep": {"optional": True}},
    )
    with pytest.raises(feed.FeedError, match="missing"):
        npm_closure.validate(tmp_path, entry)


def test_bundled_hoisted_and_aliased_dependencies_resolve_without_npm(tmp_path):
    entry = feed.entry(test_plugin_feed.entry(), signed=False)
    package(
        tmp_path,
        "node_modules/fixture",
        "fixture",
        "1.0.0",
        dependencies={"dep": "^1.0.0", "alias": "npm:original@~2.0.0"},
        optionalDependencies={"other-platform": "1.0.0"},
        scripts={"install": "never run"},
    )
    package(tmp_path, "node_modules/fixture/node_modules/dep", "dep", "1.4.0")
    package(tmp_path, "node_modules/alias", "original", "2.0.3")
    npm_closure.validate(tmp_path, entry)
    package(tmp_path, "node_modules/alias", "original", "3.0.0")
    with pytest.raises(feed.FeedError, match="satisfy"):
        npm_closure.validate(tmp_path, entry)


def test_metadata_must_match_the_declared_distribution(tmp_path):
    entry = feed.entry(test_plugin_feed.entry(), signed=False)
    package(tmp_path, "node_modules/fixture", "fixture", "2.0.0")
    with pytest.raises(feed.FeedError, match="disagrees"):
        npm_closure.validate(tmp_path, entry)


def test_required_and_peer_constraints_are_both_checked(tmp_path):
    entry = feed.entry(test_plugin_feed.entry(), signed=False)
    package(
        tmp_path,
        "node_modules/fixture",
        "fixture",
        "1.0.0",
        dependencies={"dep": "^1.0.0"},
        peerDependencies={"dep": ">=1.0.0"},
    )
    package(tmp_path, "node_modules/dep", "dep", "2.0.0")
    with pytest.raises(feed.FeedError, match="satisfy"):
        npm_closure.validate(tmp_path, entry)


@pytest.mark.parametrize(
    "bad", ["file:../evil", "latest", "https://evil.test/pkg", "workspace:*", ""]
)
@pytest.mark.parametrize("position", [0, 1])
def test_every_union_arm_is_validated_even_when_another_matches(bad, position):
    arms = ["^1", "^1"]
    arms[position] = bad
    with pytest.raises(feed.FeedError):
        npm_closure.satisfies("1.2.3", " || ".join(arms))


@pytest.mark.parametrize("missing", [False, True])
def test_closure_rejects_hidden_unsupported_optional_union(tmp_path, missing):
    entry = feed.entry(test_plugin_feed.entry(), signed=False)
    package(
        tmp_path,
        "node_modules/fixture",
        "fixture",
        "1.0.0",
        optionalDependencies={"dep": "^1 || file:../evil"},
    )
    if not missing:
        package(tmp_path, "node_modules/dep", "dep", "1.2.3")
    with pytest.raises(feed.FeedError, match="unsupported"):
        npm_closure.validate(tmp_path, entry)


@pytest.mark.parametrize(
    "version,spec,expected",
    [
        ("1.2.3", ">=1.2.3 <2.0.0 || 2.0.0-beta", True),
        ("2.0.0-beta", ">=1.2.3 <2.0.0 || 2.0.0-beta", True),
        ("2.0.0-beta.2", ">=2.0.0-beta.1 <2.0.0", True),
        ("2.0.0-beta.2", ">=1.0.0-beta.1 <3.0.0", False),
        ("2.0.0-beta.10", ">2.0.0-beta.2", True),
        ("2.0.0-beta.2", ">2.0.0-beta.10", False),
        ("2.0.0", ">2.0.0-beta", True),
        ("2.0.0-beta", "^1.2.3 >=2.0.0-beta", False),
        ("2.0.0-beta", "~1 >=2.0.0-beta", False),
        ("1.2.3-beta", "1.2.3-beta - 2.0.0", True),
        ("1.2.3", "<=*", True),
        ("1.3.0-beta", ">1.2 >=1.3.0-beta", False),
    ],
)
def test_stable_and_prerelease_union_comparators(version, spec, expected):
    assert npm_closure.satisfies(version, spec) is expected
