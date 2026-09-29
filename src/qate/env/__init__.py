"""Trading environments: a named directory holding one run's configuration.

`env.Env` is the directory and its lifecycle -- load the config objects, chdir
into it, take the lock, set up logging. `sys_env` is the machine around it:
where those directories live, and which credentials exist.
"""
