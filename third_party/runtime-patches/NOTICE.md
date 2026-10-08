# CPython cleanup policy

`tempfile-rmtree-original.txt` is the exact method from CPython 3.14.8,
commit `8e6e75d9102e39bed2a2b279203a396741180f12`,
[Lib/tempfile.py](https://github.com/python/cpython/blob/8e6e75d9102e39bed2a2b279203a396741180f12/Lib/tempfile.py).
The original full source SHA-256 is
`3714372b63f6bc05a5273425d446f6ad3a2112a659c71c321bb4575fd082d634`.
The original method SHA-256 is
`48fd4fb808ef3455995a5f94b5770596d85b3c9512517cec4569a9bc7fd46771`.
The adjacent unmodified CPython license is retained from the same commit.

Knowledge Layer modifies only `TemporaryDirectory._rmtree` in its pinned Linux
images. It delegates cleanup to descriptor-safe `shutil.rmtree`, and removes
automatic permission/flag repair. Unsupported removal refuses; inaccessible
temporary files may remain for explicit operator cleanup. Original package
versions, package databases and scanner matches remain unchanged. The build
receipt records exact before/after hashes and removes stale bytecode, including
unchecked-hash bytecode in the vendor image.

This is a platform cleanup policy, not the open CPython 3.14 backport or an
independent advisory disposition. The upstream backport warns that concurrent
permission repair remains unsafe on some platforms. See
[PR 158430](https://github.com/python/cpython/pull/158430/files).
