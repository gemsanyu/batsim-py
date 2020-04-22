import atexit
from collections import defaultdict
from shutil import which
import signal
import subprocess
import sys
import tempfile
from typing import Callable
from typing import Sequence
from typing import Dict
from typing import List
from typing import Tuple
from typing import Any
from typing import DefaultDict
from typing import Optional

from .dispatcher import dispatch
from .events import SimulatorEvent
from .jobs import Job
from .protocol import SimulationBeginsBatsimEvent
from .protocol import NetworkHandler
from .protocol import BatsimMessage
from .protocol import ResourcePowerStateChangedBatsimEvent
from .protocol import BatsimNotifyType
from .protocol import BatsimEventType
from .protocol import JobCompletedBatsimEvent
from .protocol import JobSubmittedBatsimEvent
from .protocol import BatsimRequest
from .protocol import CallMeLaterBatsimRequest
from .protocol import KillJobBatsimRequest
from .protocol import ExecuteJobBatsimRequest
from .protocol import RejectJobBatsimRequest
from .protocol import SetResourceStateBatsimRequest
from .resources import Host
from .resources import Platform
from .utils.commons import get_free_tcp_address


class SimulatorHandler:
    """ Simulator handler class.

    This class will handle the Batsim simulation process which, includes the 
    management of resources and jobs.

    Args:
        tcp_address: An address string consisting of three parts  as follows: 
            protocol://interface:port.

    Raises:
        ImportError: In case of Batsim is not installed or cannot be found.

    Examples:
        >>> handler = SimulatorHandler("tcp://localhost:28000")
    """

    def __init__(self, tcp_address: Optional[str] = None) -> None:
        if which('batsim') is None:
            raise ImportError('(HINT: you need to install Batsim. '
                              'Check the setup instructions here: '
                              'https://batsim.readthedocs.io/en/latest/.). '
                              'Run "batsim --version" to make sure it is working.')

        self.__network = NetworkHandler(tcp_address or get_free_tcp_address())
        self.__current_time: float = 0.
        self.__simulator: Optional[subprocess.Popen] = None
        self.__simulation_time: Optional[float] = None
        self.__platform: Optional[Platform] = None
        self.__no_more_jobs_to_submit = False
        self.__batsim_requests: List[BatsimRequest] = []
        self.__jobs: List[Job] = []
        self.__callbacks: DefaultDict[float,
                                      List[Callable[[float], None]],
                                      ] = defaultdict(list)

        # Batsim events handlers
        self.__batsim_event_handlers: Dict[Any, Any] = {
            BatsimEventType.SIMULATION_ENDS: self.__on_batsim_simulation_ends,
            BatsimEventType.SIMULATION_BEGINS: self.__on_batsim_simulation_begins,
            BatsimEventType.JOB_COMPLETED: self.__on_batsim_job_completed,
            BatsimEventType.JOB_SUBMITTED: self.__on_batsim_job_submitted,
            BatsimEventType.RESOURCE_STATE_CHANGED: self.__on_batsim_host_state_changed,
            BatsimEventType.REQUESTED_CALL: self.__on_batsim_requested_call,
            BatsimNotifyType.NO_MORE_STATIC_JOB_TO_SUBMIT: self.__on_batsim_no_more_jobs_to_submit
        }

        atexit.register(self.__close_simulator)
        signal.signal(signal.SIGTERM, self.__on_sigterm)

    @property
    def jobs(self) -> Sequence[Job]:
        """ A sequence with all jobs in the system. 

        This includes only jobs that are waiting in the queue or running.
        """
        return list(self.__jobs)

    @property
    def queue(self) -> Sequence[Job]:
        """ A sequence of all jobs waiting in the queue."""
        return [j for j in self.__jobs if j.is_submitted]

    @property
    def agenda(self) -> Optional[Sequence[Tuple[Host, Sequence[Job]]]]:
        """ A sequence of hosts with their jobs queue."""
        if self.__platform:
            return [(h, h.jobs) for h in self.__platform]
        else:
            return None

    @property
    def platform(self) -> Optional[Platform]:
        """ The simulation platform."""
        return self.__platform

    @property
    def is_running(self) -> bool:
        """ Whether the simulation is running."""
        return self.__simulator is not None

    @property
    def current_time(self) -> float:
        """ The current simulation time. """
        return float(f"{self.__current_time:.1f}")

    @property
    def is_submitter_finished(self) -> bool:
        """ Whether there are still some jobs to be submitted. 

        In other words, it tells if the workload has finished.
        """
        return self.__no_more_jobs_to_submit

    def start(self,
              platform: str,
              workload: str,
              verbosity: Optional[str] = "quiet",
              simulation_time: Optional[float] = None) -> None:
        """ Starts the simulation process.

        It'll load the platform and submit the jobs defined in the workload.

        Args:
            platform: The XML file describing the platform. It must follow the
                format expected by Batsim and SimGrid. Check their documentation
                on how to describe a platform.
            workload: A JSON file describing the jobs and their profiles.
                The simulation process will only submit the jobs that are
                defined in this json. Moreover, the Batsim is responsable for
                the submission process. 
            verbosity: The Batsim verbosity level. Defaults to "quiet". Available 
                values: quiet, network-only, information, debug. It controls the
                verbosity of the Batsim only.
            simulation_time: The maximum simulation time. Defaults to None.
                If this argument is set, the simulation will stop only when this
                time is reached, no matter if there are jobs to be submitted or 
                running. Otherwise, the simulation will only stop when all jobs
                in the workload were submitted and completed/rejected.

        Raises:
            ValueError: In case of invalid arguments value.
            RuntimeError: In case of invalid platform description or the 
                simulation is already running or the platform could not be
                loaded.
            NotImplementedError: In case of not supported properties in the
                platform description.

        Dispatch:
            A simulation begins event.

        Examples:
            >>> handler = SimulatorHandler("tcp://localhost:28000")
            >>> handler.start("platform.xml", "workload.json", "information", 1440)

        """
        if self.is_running:
            raise RuntimeError("The simulation is already running.")

        self.__jobs = []
        self.__current_time = 0.
        self.__simulation_time = simulation_time
        self.__no_more_jobs_to_submit = False

        cmd = ('batsim -E --forward-profiles-on-submission'
               ' --disable-schedule-tracing '
               ' --disable-machine-state-tracing')

        cmd += " -s {} -p {} -w {} -v {}".format(
            self.__network.address, platform, workload, verbosity)

        # There isn't an option to avoid exporting batsim results
        cmd += " -e {}".format(tempfile.gettempdir() + "/batsim")

        self.__simulator = subprocess.Popen(cmd.split(),
                                            stdout=subprocess.PIPE)

        self.__network.bind()
        self.__handle_batsim_events()

        if not self.__platform:
            raise RuntimeError("Could not load platfrom from Batsim.")

        if self.__simulation_time:
            self.__set_batsim_call_me_later(self.__simulation_time)

        dispatch(SimulatorEvent.SIMULATION_BEGINS, self)

    def close(self) -> None:
        """ Close the simulation process.

        Dispatch:
            A simulation ends event.
        """
        if not self.is_running:
            return
        self.__close_simulator()
        self.__network.close()
        self.__simulation_time = None
        self.__batsim_requests.clear()
        self.__callbacks.clear()
        dispatch(SimulatorEvent.SIMULATION_ENDS, self)

    def proceed_time(self, time: Optional[float] = None) -> None:
        """ Proceed the simulation process to the next event or time.

        Args:
            time: The time to proceed. Defaults to None.
                It's possible to proceed directly to the next event or to
                a specific time. It allows the implementation of policies
                that acts periodically or only when a specific event happened
                (like, a job submission or a job completed). For the latter 
                case, time must be None.

        Raises:
            ValueError: In case of invalid arguments value.
            RuntimeError: In case of the simulation is not running or
                a deadlock happened. The latter case occurs only when 
                there are no more events to happen and no time was specified. 
                Consequently, the simulation does not know what to do and a 
                deadlock error is raised.
        """

        def unflag(_):
            # this a internal function to be called by the callback procedure.
            self.__wait_callback = False

        if not self.is_running:
            raise RuntimeError("The simulation is not running.")

        if time and time <= 0:
            raise ValueError('Expected `time` argument to be a number '
                             'greater than zero, got {}.'.format(time))

        if not time:
            # Go to the next event.
            self.__wait_callback = False
        elif not self.__simulation_time and self.is_submitter_finished and not self.__jobs:
            # There are no more actions to do. Go to the next event.
            self.__wait_callback = False
        else:
            # Setup a call me later request.
            self.__wait_callback = True
            self.set_callback(time + self.current_time, unflag)

        self.__goto_next_batsim_event()
        self.__start_runnable_jobs()
        while self.is_running and self.__wait_callback:
            self.__goto_next_batsim_event()
            self.__start_runnable_jobs()

    def set_callback(self, at: float, call: Callable[[float], None]) -> None:
        """ Setup a callback.

        The simulation will call the function at the defined time.

        Args:
            at: The time to call the function.
            call: The function to be called. The function must accept the current 
                simulation time as argument.

        Raises:
            ValueError: In case of invalid arguments value.
            RuntimeError: In case of the simulation is not running.
            TypeError: In case the call function is not callable.
        """

        if not self.is_running:
            raise RuntimeError("The simulation is not running.")

        if at <= self.current_time:
            raise ValueError('Expected `at` argument to be a number '
                             'greater than the current simulation time'
                             ', got {}.'.format(at))

        if not callable(call):
            raise TypeError('Expected `call` argument to be callable ')

        self.__callbacks[at].append(call)
        self.__set_batsim_call_me_later(at)

    def allocate(self, job_id: str, hosts_id: Sequence[int]) -> None:
        """ Allocate resources for a job.

        To start computing, a job must allocate some resources first.
        When this resources are ready, the job will automatically starts.
        Thus, the allocate must be the result of the scheduling policy.

        Args:
            job_id: The job id.
            hosts_id: The sequence of hosts ids to be allocated for the job.

        Raises:
            RuntimeError: In case of the simulation is not running.
            LookupError: In case of job or resource not found.
        """
        if not self.is_running:
            raise RuntimeError("The simulation is not running.")

        if not self.__platform:
            raise SystemError("For some reason, the platform was not loaded")

        job = next((j for j in self.__jobs if j.id == job_id), None)
        if not job:
            raise LookupError("The job {} was not found.".format(job_id))

        # Allocate
        for h_id in hosts_id:
            host = self.__platform.get(h_id)
            host._allocate(job)

        job._allocate(hosts_id)

        # Start
        self.__start_runnable_jobs()

    def kill_job(self, job_id: str) -> None:
        """ Kill a job.

        This job can be running or waiting in the queue. This is different from 
        the reject request in that a job cannot be rejected when it's running. 

        Args:
            job_id: The job id.

        Raises:
            RuntimeError: In case of the simulation is not running.
            LookupError: In case of job not found.
        """
        if not self.is_running:
            raise RuntimeError("The simulation is not running.")

        job = next((j for j in self.__jobs if j.id == job_id), None)
        if not job:
            raise LookupError("The job {} was not found.".format(job_id))

        self.__jobs.remove(job)

        # Sync Batsim
        request = KillJobBatsimRequest(self.current_time, job_id)
        self.__batsim_requests.append(request)

    def reject_job(self, job_id: str) -> None:
        """ Reject a job.

        A rejected job will not be scheduled nor accounted. Only jobs in the
        queue can be rejected

        Args:
            job_id: The job id.

        Raises:
            RuntimeError: In case of the simulation is not running or a invalid
                job is selected.
            LookupError: In case of job not found.
        """

        if not self.is_running:
            raise RuntimeError("The simulation is not running.")

        job = next((j for j in self.__jobs if j.id == job_id), None)
        if not job:
            raise LookupError("The job {} was not found.".format(job_id))

        if not job.is_submitted:
            raise RuntimeError('Only jobs in the queue can be rejected, '
                               'got {}.'.format(job.state))

        self.__jobs.remove(job)

        # Sync Batsim
        request = RejectJobBatsimRequest(self.current_time, job_id)
        self.__batsim_requests.append(request)

    def switch_on(self, hosts_id: Sequence[int]) -> None:
        """ Switch on hosts.

        Args:
            hosts_id: The sequence of hosts id to be switched on.

        Raises:
            RuntimeError: In case of the simulation is not running or the host
                cannot be switched on because there is no power state defined 
                in the platform or it is not actually sleeping.
            LookupError: In case of host not found or power state could not be found.
        """
        if not self.is_running:
            raise RuntimeError("The simulation is not running.")

        if not self.__platform:
            raise SystemError("For some reason, the platform was not loaded.")

        for h_id in hosts_id:
            host = self.__platform.get(h_id)
            host._switch_on()
            ending_pstate = host.get_default_pstate()

            # Sync Batsim
            self.__set_batsim_host_pstate(host.id, ending_pstate.id)

    def switch_off(self, hosts_id: Sequence[int]) -> None:
        """ Switch off hosts.

        Args:
            hosts_id: The sequence of hosts id to be switched off.

        Raises:
            RuntimeError: In case of the simulation is not running or the host
                cannot be switched off because there is no power state defined 
                in the platform or it is not actually idle.
            LookupError: In case of host not found or power state could not be found.
        """
        if not self.is_running:
            raise RuntimeError("The simulation is not running.")

        if not self.__platform:
            raise SystemError("For some reason, the platform was not loaded.")

        for h_id in hosts_id:
            host = self.__platform.get(h_id)
            host._switch_off()
            ending_pstate = host.get_sleep_pstate()

            # Sync Batsim
            self.__set_batsim_host_pstate(host.id, ending_pstate.id)

    def switch_power_state(self, host_id: int, pstate_id: int) -> None:
        """ Switch the computation power of host.

        This is useful if you want to act like a DVFS policy.

        Args:
            host_id: The host id.
            pstate_id: The computation power state id.

        Raises:
            RuntimeError: In case of the simulation is not running or the 
                power state is invalid or was not found or 
                the current host state is not idle nor computing.
            LookupError: In case of host not found or power state could 
                not be found or power state is invalid.
        """
        if not self.is_running:
            raise RuntimeError("The simulation is not running.")

        if not self.__platform:
            raise SystemError("For some reason, the platform was not loaded.")

        host = self.__platform.get(host_id)
        host._set_computation_pstate(pstate_id)

        # Sync Batsim
        assert host.pstate
        self.__set_batsim_host_pstate(host.id, host.pstate.id)

    def __start_runnable_jobs(self) -> None:
        """ Start runnable jobs.

        This is an internal method used to starts jobs that were allocated.
        A job can only starts if the hosts are idle. Thus, this method ensures
        that the host can compute the job.

        Raises:
            SystemError: In case of the job has no allocated resources or the 
                platform was not loaded.
        """
        if not self.is_running:
            return

        if not self.__platform:
            raise SystemError("For some reason, the platform was not loaded.")

        runnable_jobs = [j for j in self.__jobs if j.is_runnable]
        for job in runnable_jobs:
            if not job.allocation:
                raise SystemError('For some reason, the job has no resources to '
                                  'start, got {}.'.format(job))

            is_ready = True
            # Check if all hosts are active and switch on sleeping hosts
            for h_id in job.allocation:
                host = self.__platform.get(h_id)

                if not host.is_idle:
                    is_ready = False

                if host.is_sleeping:
                    self.switch_on([host.id])

            if is_ready:
                job._start(self.current_time)

                # Sync Batsim
                request = ExecuteJobBatsimRequest(
                    self.current_time, job.id, job.allocation)
                self.__batsim_requests.append(request)

    def __goto_next_batsim_event(self) -> None:
        """ Go to the next Batsim event. """
        self.__send_requests()
        self.__handle_batsim_events()
        if self.__simulation_time and self.current_time >= self.__simulation_time:
            self.close()

    def __close_simulator(self) -> None:
        """ Close the simulator process. """
        if self.__simulator:
            self.__simulator.terminate()
            outs, errs = self.__simulator.communicate()
            self.__simulator = None

    def __set_batsim_call_me_later(self, at: float) -> None:
        """ Setup a call me later request. """
        if at <= self.current_time:
            return
        request = CallMeLaterBatsimRequest(self.current_time, at)
        if not any(isinstance(r, CallMeLaterBatsimRequest) and r.at == request.at for r in self.__batsim_requests):
            self.__batsim_requests.append(request)

    def __set_batsim_host_pstate(self, host_id: int, pstate_id: int) -> None:
        """ Set Batsim host power state. """
        def get_old_request() -> Optional[SetResourceStateBatsimRequest]:
            """ Get the request with the same properties. """
            for r in self.__batsim_requests:
                if r.timestamp != self.current_time:
                    continue
                elif not isinstance(r, SetResourceStateBatsimRequest):
                    continue
                elif r.state != pstate_id:
                    continue
                else:
                    return r
            return None

        # We try to minimize the number of requests.
        request = get_old_request()

        if request:
            request.add_resource(host_id)
        else:
            request = SetResourceStateBatsimRequest(
                self.current_time, [host_id], pstate_id)
            self.__batsim_requests.append(request)

    def __handle_batsim_events(self) -> None:
        """ Handle Batsim events. """
        msg = self.__network.recv()
        for event in msg.events:
            self.__current_time = event.timestamp
            if event.type in self.__batsim_event_handlers:
                handler = self.__batsim_event_handlers[event.type]
                handler(event)

        self.__current_time = msg.now

    def __send_requests(self) -> None:
        """ Send Batsim requests. """
        msg = BatsimMessage(self.current_time, self.__batsim_requests)
        self.__network.send(msg)
        self.__batsim_requests.clear()

    def __on_batsim_simulation_begins(self, event: SimulationBeginsBatsimEvent) -> None:
        self.__platform = event.platform

    def __on_batsim_simulation_ends(self, _) -> None:
        """ Handle batsim simulation ends event. """
        if self.__simulator:
            ack = BatsimMessage(self.current_time, [])
            self.__network.send(ack)
            self.__simulator.wait(5)
        self.close()

    def __on_batsim_host_state_changed(self, event: ResourcePowerStateChangedBatsimEvent) -> None:
        """ Handle batsim host state changed event. 

        When a host is switched on/off, the batsim simulates the transition costs
        and tells the scheduler only when the host is sleeping or idle. Thus, 
        Batsim is the responsable to tell when the host finished its transition.
        """
        if not self.__platform:
            raise SystemError("For some reason, the platform was not loaded.")

        for h_id in event.resources:
            h = self.__platform.get(h_id)
            assert h.pstate

            if h.is_switching_off:
                h._set_off()
            elif h.is_switching_on:
                h._set_on()
            elif (h.is_idle or h.is_computing) and h.pstate.id != event.state:
                h._set_computation_pstate(int(event.state))

            if h.pstate.id != event.state:
                raise SystemError('For some reason, the internal platform differs '
                                  'from the Batsim platform, got pstate {} while '
                                  'batsim got pstate {}.'.format(h.pstate.id, event.state))

        self.__start_runnable_jobs()

    def __on_batsim_requested_call(self, _) -> None:
        """ Handle batsim answer to call me back request.  """
        if self.current_time in self.__callbacks:
            for callback in self.__callbacks[self.current_time]:
                callback(self.current_time)
            del self.__callbacks[self.current_time]

    def __on_batsim_job_submitted(self, event: JobSubmittedBatsimEvent) -> None:
        """ Handle batsim job submitted event.  """
        self.__jobs.append(event.job)
        event.job._submit(self.current_time)

    def __on_batsim_job_completed(self, event: JobCompletedBatsimEvent) -> None:
        """ Handle batsim job submitted event.  """

        job = next((j for j in self.__jobs if j.id == event.job_id), None)
        if not job:
            raise SystemError("The job {} was not found.".format(event.job_id))

        job._terminate(self.current_time, event.job_state)
        self.__jobs.remove(job)
        self.__start_runnable_jobs()

    def __on_batsim_no_more_jobs_to_submit(self, _) -> None:
        """ Handle batsim submitter finished event.  """
        self.__no_more_jobs_to_submit = True

    def __on_sigterm(self, signum, frame) -> None:
        """ Close simulation on sigterm.  """
        self.__close_simulator()
        sys.exit(signum)
