
#include <pthread.h>
#include <sched.h>

static int
set_python_thread_affinity(void)
{
    pthread_t thread = pthread_self();
    cpu_set_t cpu_set;

    if(pthread_getaffinity_np(thread, sizeof(cpu_set_t), &cpu_set) != 0) {
        /* Could be triggered by >64 HW threads */
        return 0;
    }

    CPU_ZERO(&cpu_set);
    CPU_SET(1, &cpu_set);

    if(pthread_setaffinity_np(thread, sizeof(cpu_set_t), &cpu_set) != 0) {
        /* Parent affinity excludes CPU1 or CPU1 does not exist (usually implies single core system) */
        return 0;
    }

    return 1;
}
