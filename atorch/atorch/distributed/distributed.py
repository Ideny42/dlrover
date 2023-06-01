import multiprocessing as mp
import os
import sys
from datetime import timedelta

import numpy as np
import torch
import torch.distributed as dist
import torch.distributed.rpc as torch_rpc
from distutils.util import strtobool
from torch.distributed.distributed_c10d import _get_default_group

from atorch.common.log_utils import default_logger as logger
from atorch.common.util_func import find_free_port, get_ip_address, wait_for_server_started


class _DistributedContext:
    LOCAL_RANK = None
    RANK = None
    WORLD_SIZE = None
    BACKEND = None
    INITIALIZED = False
    PG_NAME_PREFIX = ""
    COWORKER_SIZE = None
    NPROC_PER_NODE = None
    NODE_SIZE = None
    PARALLEL_GROUP_SIZE = None
    PARALLEL_RANK = None
    PARALLEL_GROUP = None
    PARALLEL_GROUPS_AND_RANKS = None
    PARALLEL_CONFIG = None
    COWORKER_NUM_PER_NODE = None
    STORE = None
    PREFIX_STORE_COUNT = 0
    PIPE_RPC_INIT = 0


class _CoworkerContext:
    GPU_POD_ADDRS = None
    COWORKER_ADDRS = None
    USE_ELASTIC_DATALOADER = False
    DATA_INFO_SERVER = None


class ParallelGroupContextManager:
    def __init__(self, name=""):
        self.name = name
        self.old_name = ""

    def __enter__(self):
        self.old_name = _DistributedContext.PG_NAME_PREFIX
        _DistributedContext.PG_NAME_PREFIX = self.name

    def __exit__(self, exc_type, exc_value, exc_tb):
        _DistributedContext.PG_NAME_PREFIX = self.old_name


def _prefix_pg_name(name):
    return _DistributedContext.PG_NAME_PREFIX + name


def local_rank():
    if _DistributedContext.LOCAL_RANK is not None:
        return _DistributedContext.LOCAL_RANK

    return 0 if not torch.distributed.is_initialized() else int(os.getenv("LOCAL_RANK", -1))


def rank():
    if _DistributedContext.RANK is not None:
        return _DistributedContext.RANK
    else:
        return torch.distributed.get_rank() if torch.distributed.is_initialized() else None


def world_size():
    return _DistributedContext.WORLD_SIZE or (
        torch.distributed.get_world_size() if torch.distributed.is_initialized() else None
    )


def parallel_group(name):
    if _DistributedContext.PARALLEL_GROUP is not None and _prefix_pg_name(name) in _DistributedContext.PARALLEL_GROUP:
        return _DistributedContext.PARALLEL_GROUP[_prefix_pg_name(name)]
    return None


def parallel_group_and_ranks(name):
    if (
        _DistributedContext.PARALLEL_GROUPS_AND_RANKS is not None
        and _prefix_pg_name(name) in _DistributedContext.PARALLEL_GROUPS_AND_RANKS
    ):
        groups_and_ranks = _DistributedContext.PARALLEL_GROUPS_AND_RANKS[_prefix_pg_name(name)]
        for group, ranks in groups_and_ranks:
            if rank() in ranks:
                return group, ranks
        return None, None
    else:
        cur_group = parallel_group(name)
        cur_ranks = list(range(world_size())) if cur_group is not None else None
        return cur_group, cur_ranks


def parallel_rank(name):
    if _DistributedContext.PARALLEL_RANK is not None and _prefix_pg_name(name) in _DistributedContext.PARALLEL_RANK:
        return _DistributedContext.PARALLEL_RANK[_prefix_pg_name(name)]
    return None


def parallel_config():
    return _DistributedContext.PARALLEL_CONFIG


def parallel_group_size(name):
    if (
        _DistributedContext.PARALLEL_GROUP_SIZE is not None
        and _prefix_pg_name(name) in _DistributedContext.PARALLEL_GROUP_SIZE
    ):
        return _DistributedContext.PARALLEL_GROUP_SIZE[_prefix_pg_name(name)]
    return None


def backend():
    return _DistributedContext.BACKEND


def coworker_size():
    return _DistributedContext.COWORKER_SIZE


def nproc_per_node():
    return _DistributedContext.NPROC_PER_NODE


def node_size():
    return _DistributedContext.NODE_SIZE


def coworker_num_per_node():
    return _DistributedContext.COWORKER_NUM_PER_NODE


def use_coworker():
    return _DistributedContext.COWORKER_NUM_PER_NODE is not None and _DistributedContext.COWORKER_NUM_PER_NODE > 0


def is_coworker():
    return use_coworker() and local_rank() < coworker_num_per_node()


def coworker_local_rank():
    if is_coworker():
        return local_rank()
    else:
        return None


def worker_local_rank():
    if use_coworker() and local_rank() >= coworker_num_per_node():
        return local_rank() - coworker_num_per_node()
    else:
        return None


def worker_num_per_node():
    if use_coworker():
        return nproc_per_node() - coworker_num_per_node()
    else:
        return nproc_per_node()


def gpu_pod_addrs():
    return _CoworkerContext.GPU_POD_ADDRS


def coworker_addrs():
    return _CoworkerContext.COWORKER_ADDRS


def _get_data_info_server():
    return _CoworkerContext.DATA_INFO_SERVER


def _check_env(elastic_or_fault_tolerant=False):
    local_rank = os.getenv("LOCAL_RANK")
    if not local_rank:
        logger.warning("LOCAL_RANK env not set. Set as 0")
        os.environ["LOCAL_RANK"] = "0"

    rank = os.getenv("RANK")
    if not rank:
        logger.warning("RANK env not set. Set as 0")
        os.environ["RANK"] = "0"

    world_size = os.getenv("WORLD_SIZE")
    if not world_size:
        logger.warning("WORLD_SIZE env not set. Set as 1")
        os.environ["WORLD_SIZE"] = "1"

    master_addr = os.getenv("MASTER_ADDR")
    if not master_addr:
        logger.warning("MASTER_ADDR env not set. Set as 127.0.0.1")
        os.environ["MASTER_ADDR"] = "127.0.0.1"

    master_port = os.getenv("MASTER_PORT")
    if not master_port:
        port = find_free_port()
        logger.warning("MASTER_PORT env not set. Set as {}".format(port))
        os.environ["MASTER_PORT"] = str(port)

    if not elastic_or_fault_tolerant:
        nproc_per_node = os.getenv("NPROC_PER_NODE")
        if not nproc_per_node:
            logger.warning("NPROC_PER_NODE env not set. Set as 1")
            os.environ["NPROC_PER_NODE"] = "1"
    else:
        os.environ["NPROC_PER_NODE"] = os.getenv("LOCAL_WORLD_SIZE")

    master_port2 = os.getenv("MASTER_PORT2")
    if not master_port2:
        port = find_free_port()
        logger.warning("MASTER_PORT2 env not set. Set as {}".format(port))
        os.environ["MASTER_PORT2"] = str(port)

    coworker_size = os.getenv("COWORKER_SIZE")
    if not coworker_size:
        logger.warning("COWORKER_SIZE env not set. Set as 0")
        os.environ["COWORKER_SIZE"] = "0"

    node_size = os.getenv("NODE_SIZE")
    if not node_size:
        logger.warning("NODE_SIZE env not set. Set as 1")
        os.environ["NODE_SIZE"] = "1"


def get_pg_ranks(slicing_dim, rank_order):
    pg_ranks = {}
    stride = 1
    total_size = np.prod([p[1] for p in slicing_dim])
    for (name, size) in slicing_dim:
        mask = [True] * total_size
        ranks_list = []
        index = 0
        while index < total_size:
            if mask[index] is False:
                index += 1
                continue
            ranks = []
            next_index = index
            for i in range(size):
                ranks.append(rank_order[next_index])
                assert mask[next_index] is True
                mask[next_index] = False
                next_index += stride
            index += 1
            ranks_list.append(ranks)
        pg_ranks[name] = ranks_list
        stride *= size
    return pg_ranks


def pair_data_group_with_pipe_stage_id():
    if _prefix_pg_name("pipe") in _DistributedContext.PARALLEL_GROUPS_AND_RANKS:
        # any pipe ranks suffices as we are only matching groups with groups
        _, pipe_ranks = _DistributedContext.PARALLEL_GROUPS_AND_RANKS[_prefix_pg_name("pipe")][0]
        stage_to_data_group = {}
        data_group_name = "zero" if _prefix_pg_name("zero") in _DistributedContext.PARALLEL_GROUPS_AND_RANKS else "data"
        for data_group, data_ranks in _DistributedContext.PARALLEL_GROUPS_AND_RANKS[_prefix_pg_name(data_group_name)]:
            common_ranks = list(set(data_ranks) & set(pipe_ranks))
            if len(common_ranks) > 1 or len(common_ranks) == 0:
                logger.warning("gpu not correctly partitioned")
            else:
                stage_id = pipe_ranks.index(common_ranks[0])
                stage_to_data_group[stage_id] = data_group
        return stage_to_data_group
    else:
        logger.warning("Not using pipe, cannot generate mapping between stage and data group")
        return None


def create_parallel_group(parallel_config):
    """
    Create additional groups for mixed parallel when needed.
    parallel_config: (List[Tuple[str, int]], Optional(List(int)))
    The first item is a list of (name, size) for mixed parallel. MUL(size) should equal to the number of processes.
    The second item for rank order, which is optional. if None, using the numeric order.
    For example, ([("tensor", 4), ("pipeline", 2), ("data", 2)], None) would create:
    4 process groups for "tensor" [0, 1, 2, 3], [4, 5, 6, 7], [8, 9, 10, 11], [12, 13, 14, 15]
    8 process groups for "pipeline" [0, 4], [1, 5], [2, 6], [3, 7], [8, 12], [9, 13], [10, 14], [11, 15]
    8 process groups for "data: [0, 8], [1, 9], [2, 10], [3, 11], [4, 12], [5, 13], [6, 14], [7, 15]
    """
    assert _DistributedContext.INITIALIZED or torch.distributed.is_initialized(), "distributed should be initialized"
    slicing_dim = parallel_config[0]
    rank_order = parallel_config[1]
    assert len(slicing_dim) > 0, "parallel_config should not be empty"
    assert world_size() == np.prod(
        [p[1] for p in slicing_dim]
    ), "Multiplication of parallel_config  sizes should equal to world_size"
    if rank_order is None:
        rank_order = list(range(world_size()))

    new_config = [slicing_dim, rank_order]
    if _DistributedContext.PARALLEL_CONFIG is None:
        _DistributedContext.PARALLEL_CONFIG = new_config
    elif isinstance(_DistributedContext.PARALLEL_CONFIG, list):
        _DistributedContext.PARALLEL_CONFIG = (_DistributedContext.PARALLEL_CONFIG, new_config)
    else:
        _DistributedContext.PARALLEL_CONFIG = _DistributedContext.PARALLEL_CONFIG + (new_config,)
    _DistributedContext.PARALLEL_GROUP_SIZE = {}
    _DistributedContext.PARALLEL_RANK = {}
    _DistributedContext.PARALLEL_GROUP = {}

    has_pipe = False

    if len(slicing_dim) == 1:
        # only one slicing dim, use global pg.
        name, size = slicing_dim[0]
        assert name not in _DistributedContext.PARALLEL_GROUP, f"group name {name} already used"
        _DistributedContext.PARALLEL_GROUP_SIZE[_prefix_pg_name(name)] = size
        _DistributedContext.PARALLEL_RANK[_prefix_pg_name(name)] = rank()
        _DistributedContext.PARALLEL_GROUP[_prefix_pg_name(name)] = _get_default_group()
        if name == "pipe":
            has_pipe = True
    else:
        all_pg_ranks = get_pg_ranks(slicing_dim, rank_order)
        _DistributedContext.PARALLEL_GROUPS_AND_RANKS = {}
        for (name, size) in slicing_dim:
            if name == "pipe":
                has_pipe = True
            _DistributedContext.PARALLEL_GROUP_SIZE[_prefix_pg_name(name)] = size
            named_ranks = all_pg_ranks[name]
            group_and_ranks = []
            for ranks in named_ranks:
                group = dist.new_group(ranks)
                group_and_ranks.append((group, ranks))
                if rank() in ranks:
                    _DistributedContext.PARALLEL_GROUP[_prefix_pg_name(name)] = group
                    _DistributedContext.PARALLEL_RANK[_prefix_pg_name(name)] = ranks.index(rank())
            _DistributedContext.PARALLEL_GROUPS_AND_RANKS[_prefix_pg_name(name)] = group_and_ranks

    if has_pipe:
        # initialize rpc for pipeline execution
        _build_pippy_rpc_networks()


def destroy_parallel_group(destroy_rpc=True):
    """
    Delete groups created for mixed parallel and reset parallel mode info
    """
    # must destroy rpc first, since pipe training might depend on this to block and synchronize
    if _DistributedContext.PIPE_RPC_INIT == 1 and destroy_rpc:
        _destroy_pippy_rpc_network()
    if _DistributedContext.PARALLEL_GROUPS_AND_RANKS is not None:
        for gnr in _DistributedContext.PARALLEL_GROUPS_AND_RANKS.values():
            for (group, _) in gnr:
                dist.destroy_process_group(group)

    _DistributedContext.PARALLEL_GROUP_SIZE = None
    _DistributedContext.PARALLEL_RANK = None
    _DistributedContext.PARALLEL_GROUP = None
    _DistributedContext.PARALLEL_GROUPS_AND_RANKS = None
    _DistributedContext.PARALLEL_CONFIG = None


def _build_pippy_rpc_networks(num_worker_threads=64, rpc_timeout=1800, init_method="env://"):
    def _has_efa():
        try:
            import subprocess

            return (
                subprocess.run(
                    ["fi_info", "-p", "efa", "-t", "FI_EP_RDM"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                ).returncode
                == 0
            )
        except FileNotFoundError:
            return False
        except PermissionError:
            return False

    if _DistributedContext.PIPE_RPC_INIT == 0:
        all_world_size = world_size()

        if _has_efa():
            logger.info("has efa")
            tp_transport = ["shm", "uv"]
        else:
            logger.info("no efa")
            tp_transport = None
        if init_method == "file":
            import tempfile

            temp_file = tempfile.NamedTemporaryFile()
            init_method = f"file://{temp_file.name}"

        options = torch_rpc.TensorPipeRpcBackendOptions(
            num_worker_threads=num_worker_threads,
            rpc_timeout=rpc_timeout,
            _transports=tp_transport,
            init_method=init_method,
        )

        # as we assume all gpus participates in the training
        # suffices to set device map for all devices

        # get the number of devices per node
        num_devices = torch.cuda.device_count()
        if num_devices > 0:
            device_id = rank() % num_devices
            for i in range(all_world_size):
                options.set_device_map(f"worker{i}", {device_id: i % num_devices})

        torch_rpc.init_rpc(f"worker{rank()}", rank=rank(), world_size=all_world_size, rpc_backend_options=options)

        _DistributedContext.PIPE_RPC_INIT = 1
        logger.info(f"Successfully build rpc network at rank {rank()}")


def _destroy_pippy_rpc_network():
    _DistributedContext.PIPE_RPC_INIT = 0
    torch_rpc.shutdown()


def _build_torch_rpc_networks(ddp_group_size):
    # init RPC group
    rpc_rank = rank()
    rpc_name = str(rpc_rank)
    rpc_world_size = world_size()
    master_addr = os.getenv("MASTER_ADDR")
    master_port2 = os.getenv("MASTER_PORT2")
    rpc_backend_options = torch_rpc.TensorPipeRpcBackendOptions(
        init_method="tcp://{}:{}".format(master_addr, master_port2),
        _transports=["uv"],
    )
    torch_rpc.init_rpc(
        rpc_name,
        rank=rpc_rank,
        world_size=rpc_world_size,
        rpc_backend_options=rpc_backend_options,
    )
    if rank() >= ddp_group_size:
        logger.info("Init coworker's rpc successfully.")
        torch_rpc.shutdown()
        logger.info("coworker's rpc shutdown")
        sys.exit(0)


def _build_grpc_networks(ddp_group_size):
    ip_address = get_ip_address()
    free_port = str(find_free_port())
    ip_and_port = ip_address + ":" + free_port
    # Calling `_get_pods_address` in the main process sometimes raise
    # segmentation fault error. Thus, call `_get_pods_address` in the
    # subprocesses to avoid this problems.
    addrs_queue = mp.SimpleQueue()
    tcpstore_process = mp.Process(
        target=_get_pods_address,
        args=(ddp_group_size, ip_and_port, rank(), addrs_queue, 900),
    )
    tcpstore_process.start()
    _CoworkerContext.GPU_POD_ADDRS = addrs_queue.get()
    _CoworkerContext.COWORKER_ADDRS = addrs_queue.get()
    tcpstore_process.join()
    if rank() < ddp_group_size and local_rank() == 0:
        # Every gpu pods' worker0 create a Data Info Service
        from atorch.service.data_info_service import create_data_info_service

        server = create_data_info_service(free_port, os.cpu_count())
        server.start()
        logger.info(f"Data Info Service is listening at {ip_and_port}")
        _CoworkerContext.DATA_INFO_SERVER = server
    elif rank() > ddp_group_size:
        # coworker 1 ~ n need to wait for dynamic data sharding
        # service(on coworker0) to start.
        coworker0_addr_and_port = coworker_addrs()[ddp_group_size]
        service_ip, service_port = coworker0_addr_and_port.split(":")
        wait_for_server_started(service_ip, int(service_port), timeout=300)


def _get_pods_address(ddp_group_size, ip_and_port, rpc_rank, addrs_queue, timeout=60):
    is_master = rpc_rank == 0
    store = dist.TCPStore(
        os.getenv("MASTER_ADDR"),
        int(os.getenv("MASTER_PORT2")),
        world_size(),
        is_master,
        timeout=timedelta(seconds=timeout),
    )
    if (rpc_rank < ddp_group_size and rpc_rank % nproc_per_node() == 0) or rpc_rank >= ddp_group_size:
        # Only coworkers and workers in gpu pods whose local_rank is 0 need
        # to set their ips
        store.set(str(rpc_rank), ip_and_port)

    coworker0_rank = ddp_group_size
    coworker0_addr = store.get(str(coworker0_rank)).decode("UTF-8")

    gpu_pods_addrs = {}
    coworkers_addrs = {coworker0_rank: coworker0_addr}
    if rpc_rank > ddp_group_size:
        coworkers_addrs[rpc_rank] = ip_and_port
    for r in range(ddp_group_size):
        if r % nproc_per_node() == 0:
            gpu_pod_addr = store.get(str(r)).decode("UTF-8")
            gpu_pods_addrs[r] = gpu_pod_addr
    addrs_queue.put(gpu_pods_addrs)

    if rpc_rank < ddp_group_size:
        for r in range(ddp_group_size + 1, world_size()):
            coworker_addr = store.get(str(r)).decode("UTF-8")
            coworkers_addrs[r] = coworker_addr
    addrs_queue.put(coworkers_addrs)

    # Make sure the rank0 waits for other ranks to finish
    if rpc_rank == 0:
        store.wait(["{}_done".format(i) for i in range(1, world_size()) if i != coworker0_rank])
    else:
        store.set("{}_done".format(rpc_rank), "True")


def init_coworker_process_groups(backend):
    # init store
    if _DistributedContext.STORE is None:
        _DistributedContext.STORE = dist.TCPStore(
            os.getenv("MASTER_ADDR"),
            int(os.getenv("MASTER_PORT")),
            world_size(),
            rank() == 0,
            timeout=timedelta(seconds=900),
        )
    node_index = rank() // nproc_per_node()
    # create prestore and init process group using it.
    if local_rank() < _DistributedContext.COWORKER_NUM_PER_NODE:
        store = dist.PrefixStore(f"coworker_{_DistributedContext.PREFIX_STORE_COUNT}_", _DistributedContext.STORE)
        backend = "gloo"  # coworker use gloo to support cpu tensor
        new_rank = coworker_local_rank() + coworker_num_per_node() * node_index
        new_world_size = node_size() * coworker_num_per_node()
    else:
        store = dist.PrefixStore(f"worker_{_DistributedContext.PREFIX_STORE_COUNT}_", _DistributedContext.STORE)
        new_rank = worker_local_rank() + worker_num_per_node() * node_index
        new_world_size = node_size() * worker_num_per_node()

    torch.distributed.init_process_group(backend, rank=new_rank, world_size=new_world_size, store=store)

    _DistributedContext.RANK = new_rank
    _DistributedContext.WORLD_SIZE = new_world_size


def init_distributed(
    backend="nccl",
    coworker_num_per_node=0,
    elastic_or_fault_tolerant=False,
    set_cuda_device_using_local_rank=False,
):
    """
    Initializes the distributed contexts. Support DDP.

    Arguments:
        backend (str): The backend to use. Supports 'nccl', 'gloo', 'accl'.
        coworker_num_per_node: if > 0, some processes in a node are used for coworker.
        elastic_or_fault_tolerant (bool): If True, supports elastic training or fault-tolerant training.
        set_cuda_device_using_local_rank (bool):
           If True, set cuda device using local rank.
    Return:
        True if initialized successfully. False otherwise.
    """

    backend = backend.lower()
    if backend not in ["nccl", "gloo", "accl"]:
        logger.error("Invalid backend {}".format(backend))
        return False

    _DistributedContext.BACKEND = backend

    _check_env(elastic_or_fault_tolerant=elastic_or_fault_tolerant)

    # init local_rank, rank, world_size, coworker_size from env
    _DistributedContext.LOCAL_RANK = int(os.getenv("LOCAL_RANK"))
    _DistributedContext.RANK = int(os.getenv("RANK"))
    _DistributedContext.WORLD_SIZE = int(os.getenv("WORLD_SIZE"))
    _DistributedContext.COWORKER_SIZE = int(os.getenv("COWORKER_SIZE"))
    _DistributedContext.NPROC_PER_NODE = int(os.getenv("NPROC_PER_NODE"))
    _DistributedContext.NODE_SIZE = int(os.getenv("NODE_SIZE"))
    _DistributedContext.COWORKER_NUM_PER_NODE = coworker_num_per_node
    _CoworkerContext.USE_ELASTIC_DATALOADER = bool(strtobool(os.getenv("USE_ELASTIC_DATALOADER", "False")))

    if coworker_num_per_node >= nproc_per_node():
        logger.error(
            f"coworker_num_per_node({coworker_num_per_node}) should be smaller than nproc_per_node ({nproc_per_node()})"
        )
        return False

    if elastic_or_fault_tolerant and coworker_size() > 0:
        logger.error("Elastic Training is not compatible with CoWorker.")
        return False

    elif coworker_num_per_node > 0:
        init_coworker_process_groups(backend)
        if not torch.distributed.is_initialized():
            logger.error("Failed to init_process_group")
            return False
    else:
        # if backend == "accl":
        #    try:
        #        # noqa: F401
        #        import torch_accl
        #    except ImportError:
        #        logger.error("import torch_accl failed")
        #        return False
        ddp_group_size = world_size() - coworker_size()
        if rank() < ddp_group_size:
            # init with init_process_group using env
            torch.distributed.init_process_group(
                backend,
                init_method="env://",
                world_size=ddp_group_size,
                rank=rank(),
            )
            if not torch.distributed.is_initialized():
                logger.error("Failed to init_process_group")
                return False
        if coworker_size() > 0:
            if _CoworkerContext.USE_ELASTIC_DATALOADER is True:
                _build_grpc_networks(ddp_group_size)
            else:
                _build_torch_rpc_networks(ddp_group_size)

    if set_cuda_device_using_local_rank:
        gpu_num = torch.cuda.device_count()
        if gpu_num == 0:
            logger.warning("No gpu found, set_cuda_device_using_local_rank ignored!")
        else:
            torch.cuda.set_device(local_rank() % gpu_num)
            logger.info("Set cuda device as {}".format(local_rank() % gpu_num))

    logger.info(
        "Distributed context initialized: "
        "rank={}, local_rank={}, world_size={}".format(rank(), local_rank(), world_size())
    )

    _DistributedContext.INITIALIZED = True
    return True


def reset_distributed():
    """
    Reset the distributed context.
    If backend is nccl or gloo, delete the process group.
    """
    if not _DistributedContext.INITIALIZED:
        return
    destroy_parallel_group()
    torch.distributed.destroy_process_group()

    if coworker_size() > 0 and rank() < world_size() - coworker_size():
        if _CoworkerContext.USE_ELASTIC_DATALOADER is False:
            torch_rpc.shutdown()
        elif local_rank() == 0:
            _get_data_info_server().stop(None)
            logger.info("Data Info Service has stopped.")

    _DistributedContext.INITIALIZED = False
    _DistributedContext.BACKEND = None
    _DistributedContext.RANK = None
    _DistributedContext.LOCAL_RANK = None
    _DistributedContext.WORLD_SIZE = None
    _DistributedContext.COWORKER_SIZE = None
    _DistributedContext.NPROC_PER_NODE = None
    _DistributedContext.NODE_SIZE = None
    _DistributedContext.COWORKER_NUM_PER_NODE = None
    _DistributedContext.PREFIX_STORE_COUNT += 1
    _CoworkerContext.GPU_POD_ADDRS = None
    _CoworkerContext.COWORKER_ADDRS = None
    _CoworkerContext.DATA_INFO_SERVER = None