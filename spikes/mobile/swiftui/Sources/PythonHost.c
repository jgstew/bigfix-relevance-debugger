// Embeds the official CPython iOS framework. Adapted from the init sequence
// in CPython's own iOS testbed (Apple/iOS/testbed/TestbedTests.m).
#include "PythonHost.h"

#include <Python/Python.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static PyObject *bridge_module = NULL;

static char *dup_error(const char *prefix) {
    // Formats the pending Python exception (if any) after `prefix`.
    char buffer[2048];
    const char *detail = "";
    PyObject *exc = PyErr_GetRaisedException();
    PyObject *text = exc ? PyObject_Str(exc) : NULL;
    if (text) detail = PyUnicode_AsUTF8(text);
    snprintf(buffer, sizeof buffer, "%s%s%s", prefix, *detail ? ": " : "", detail ? detail : "");
    Py_XDECREF(text);
    Py_XDECREF(exc);
    return strdup(buffer);
}

static int add_site_dir(const char *path) {
    PyObject *site = PyImport_ImportModule("site");
    if (!site) return -1;
    PyObject *result = PyObject_CallMethod(site, "addsitedir", "s", path);
    Py_DECREF(site);
    if (!result) return -1;
    Py_DECREF(result);
    return 0;
}

char *bfrd_python_start(const char *resource_path, const char *module) {
    char path[4096];
    PyPreConfig preconfig;
    PyConfig config;
    PyStatus status;

    PyPreConfig_InitIsolatedConfig(&preconfig);
    preconfig.utf8_mode = 1;
    status = Py_PreInitialize(&preconfig);
    if (PyStatus_Exception(status)) return strdup(status.err_msg);

    PyConfig_InitIsolatedConfig(&config);
    config.use_system_logger = 1;  // print/logging -> the unified log (Console.app)
    config.buffered_stdio = 0;
    config.write_bytecode = 0;
    snprintf(path, sizeof path, "%s/python", resource_path);
    status = PyConfig_SetBytesString(&config, &config.home, path);
    if (!PyStatus_Exception(status)) status = Py_InitializeFromConfig(&config);
    PyConfig_Clear(&config);
    if (PyStatus_Exception(status)) return strdup(status.err_msg);

    snprintf(path, sizeof path, "%s/app_packages", resource_path);
    if (add_site_dir(path) != 0) return dup_error("app_packages");
    snprintf(path, sizeof path, "%s/app", resource_path);
    if (add_site_dir(path) != 0) return dup_error("app");

    bridge_module = PyImport_ImportModule(module);
    if (!bridge_module) return dup_error("import bridge");

    PyEval_SaveThread();  // release the GIL; every call re-acquires it
    return NULL;
}

char *bfrd_python_call(const char *function, const char *json) {
    if (!bridge_module) return strdup("{\"error\": \"Python is not started\"}");
    PyGILState_STATE gil = PyGILState_Ensure();
    char *out = NULL;
    PyObject *result = PyObject_CallMethod(bridge_module, function, "s", json);
    if (result && PyUnicode_Check(result)) {
        out = strdup(PyUnicode_AsUTF8(result));
    } else {
        char *message = dup_error(function);
        size_t size = strlen(message) + 32;
        out = malloc(size);
        // Not valid JSON escaping for every message; good enough for a spike.
        snprintf(out, size, "{\"error\": \"%s\"}", message);
        free(message);
    }
    Py_XDECREF(result);
    PyGILState_Release(gil);
    return out;
}
