from __future__ import annotations

from reviewer.diff_context import slice_diff, split_by_file

DIFF = """diff --git a/app/auth.py b/app/auth.py
index 1111111..2222222 100644
--- a/app/auth.py
+++ b/app/auth.py
@@ -1,3 +1,4 @@
 import os
+TOKEN = os.environ["TOKEN"]
diff --git a/infra/Dockerfile b/infra/Dockerfile
index 3333333..4444444 100644
--- a/infra/Dockerfile
+++ b/infra/Dockerfile
@@ -1,2 +1,2 @@
-FROM python:3.12-slim
+FROM python:latest
diff --git a/README.md b/README.md
index 5555555..6666666 100644
--- a/README.md
+++ b/README.md
@@ -1 +1,2 @@
 # project
+more docs
"""


def test_split_by_file_keys_on_post_image_path():
    sections = split_by_file(DIFF)
    assert list(sections) == ["app/auth.py", "infra/Dockerfile", "README.md"]


def test_sections_are_self_contained_and_recombine():
    sections = split_by_file(DIFF)
    assert sections["app/auth.py"].startswith("diff --git a/app/auth.py")
    assert 'TOKEN = os.environ["TOKEN"]' in sections["app/auth.py"]
    assert "Dockerfile" not in sections["app/auth.py"]
    assert "".join(sections.values()) == DIFF


def test_slice_diff_selects_requested_files_only():
    sliced = slice_diff(DIFF, ["infra/Dockerfile"])
    assert "FROM python:latest" in sliced
    assert "app/auth.py" not in sliced
    assert "README.md" not in sliced


def test_slice_diff_preserves_requested_order_and_multiplicity():
    sliced = slice_diff(DIFF, ["README.md", "app/auth.py"])
    assert sliced.index("README.md") < sliced.index("app/auth.py")


def test_empty_scope_returns_whole_diff():
    assert slice_diff(DIFF, []) == DIFF


def test_unknown_paths_fall_back_to_whole_diff():
    # A specialist reviewing everything is wasteful; one reviewing nothing is useless.
    assert slice_diff(DIFF, ["does/not/exist.py"]) == DIFF


def test_non_diff_input_is_passed_through():
    assert split_by_file("not a diff") == {}
    assert slice_diff("not a diff", ["a.py"]) == "not a diff"
