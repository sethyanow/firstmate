# Python standard

These rules apply to Python under `python/`.
`bin/fm-lint.sh` runs ruff and ty with the settings in `python/pyproject.toml`.
The rules below cover what those tools cannot see.

1. **Use Python types, not shell strings.**
   A pid is an `int`, and "no pid" is `None`, not `""`.
   Convert once, at the edge where a record or command output is read, so no function has to accept both forms.
2. **A function returns its result.**
   Return a value, or a small frozen dataclass when there are several, instead of leaving results in attributes.
   A result left in an attribute hides which call produced it and how long it stays valid.
3. **Name outcomes.**
   Use an `Enum` for an outcome, not 0, 1, or 2 by meaning, so each branch says what happened.
   A numeric exit status belongs only at the process edge.
4. **Decide on facts, then act.**
   Write a decision as a function of plain values.
   The code that reads files and runs commands gathers those values and acts on the answer, so a unit test can prove the decision without starting the program.
5. **A constructor does no I/O.**
   A constructor that reads files or runs commands makes every test that builds the object depend on those resources.
   Do the I/O before construction and pass the results in.
6. **One owner per record format.**
   Read and write each file or record format in one module.
   Readers and writers spread across a class drift apart when only one of them changes.
7. **Say a repeated check once.**
   Give a check that appears in more than one place a name of its own, so a change to it happens in one place.
8. **Small classes and modules.**
   Split a large class or module by concern.
   A module over about 500 lines needs a recorded reason, and `bin/fm-lint.sh` checks for it.
9. **Make control flow visible.**
   Annotate a function that always raises or exits with `-> NoReturn`.
   Without it, neither a reader nor the type checker can see that the call never returns.
10. **No computed attribute names and no test hooks in production code.**
    `setattr` with a computed name hides the attribute from every reader and checker.
    Pass a boundary such as a clock in, instead of reading a test setting from the environment.

Python tests live in `python/tests` and use `unittest`.

## Comments and docstrings

A comment states what the code cannot.
A docstring states the contract in a sentence or two, for example: "Lines as awk and sed see them: an unterminated last line counts."
A silent `except ...: pass` needs a comment that says why losing that error is safe.
