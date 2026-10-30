// The whole C surface Swift sees: start CPython, call a bridge function.
// Kept tiny so Swift never touches CPython macros or refcounts.
#ifndef PYTHON_HOST_H
#define PYTHON_HOST_H

// Initialise the embedded interpreter (official python.org Python.xcframework)
// from <bundle>/python, add <bundle>/app_packages and <bundle>/app to the
// path, import `module`, then release the GIL. Returns NULL on success, else
// a malloc'd error message the caller frees.
char *bfrd_python_start(const char *resource_path, const char *module);

// Call `module.<function>(json)` and return its str result, malloc'd UTF-8
// (caller frees). Safe from any thread: takes the GIL for the call.
char *bfrd_python_call(const char *function, const char *json);

#endif
