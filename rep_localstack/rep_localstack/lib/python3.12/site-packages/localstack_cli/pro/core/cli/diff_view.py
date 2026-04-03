from localstack_cli.cli import console


def print_diff(operations: dict[str, list[dict]]) -> None:
    """
    Prints the list of operations produced by a Cloud Pod loaded into the application runtime in a summarized and
    human-readable way.
    The input has the following shape:
    { "service_name": list[localstack.pro.core.persistence.pods.diff.models.Operation] }
    :param operations: a map with list of operation associated to services.
    """
    if not operations:
        console.print("This load operation does not affect the runtime state.")
        return

    # If there is any modification in the list of operations, we are facing a conflict.
    has_modification = any(
        op_dict.get("operation_type") == "MODIFICATION"
        for op_list in operations.values()
        for op_dict in op_list
    )
    if has_modification:
        console.print(
            "[yellow]This load operation modifies one or more resources in the"
            " application runtime."
            " The result will depend on the selected merge strategy."
            " Use the --help option to read more about it.[/]\n"
        )

    console.print("This load operation will modify the runtime state as follows:")
    for _service, _operations in operations.items():
        addition_operations = [ops for ops in _operations if ops["operation_type"] == "ADDITION"]
        modification_operations = [
            ops for ops in _operations if ops["operation_type"] == "MODIFICATION"
        ]

        if not addition_operations and not modification_operations:
            return
        console.rule(_service)
        console.print(f"[green]+[/] {len(addition_operations)} resources added.")
        console.print(f"[yellow]~[/] {len(modification_operations)} resources modified.")
