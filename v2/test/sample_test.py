# Copyright 2021 The Kubeflow Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# %%
import yaml
import os
import kfp

REPO_ROOT = os.path.join('..', '..')
SAMPLES_CONFIG_PATH = os.path.join(REPO_ROOT, 'samples', 'test', 'config.yaml')
SAMPLES_CONFIG = None
with open(SAMPLES_CONFIG_PATH, 'r') as stream:
    SAMPLES_CONFIG = yaml.safe_load(stream)

download_gcs_tgz = kfp.components.load_component_from_file(
    'components/download_gcs_tgz.yaml'
)
run_sample = kfp.components.load_component_from_file(
    'components/run_sample.yaml'
)
kaniko = kfp.components.load_component_from_file('components/kaniko.yaml')

PIPELINE_TIME_OUT = 40 * 60  # 40 minutes


@kfp.dsl.pipeline(name='v2 sample test')
def v2_sample_test(
    context: 'URI' = 'gs://your-bucket/path/to/context.tar.gz',
    gcs_root: 'URI' = 'gs://ml-pipeline-test/v2',
    image_registry: 'URI' = 'gcr.io/ml-pipeline-test',
    kfp_host: 'URI' = 'http://ml-pipeline:8888',
    samples_config: list = SAMPLES_CONFIG,
):
    # pipeline configs
    conf = kfp.dsl.get_pipeline_conf()
    conf.set_timeout(
        PIPELINE_TIME_OUT
    )  # add timeout to avoid pipelines stuck in running leak indefinetely

    download_src_op = download_gcs_tgz(
        gcs_path=context
    ).set_cpu_limit('0.5').set_memory_limit('500Mi'
                                           ).set_display_name('download_src')
    download_src_op.execution_options.caching_strategy.max_cache_staleness = "P0D"

    def build_image(name: str, dockerfile: str) -> kfp.dsl.ContainerOp:
        task: kfp.dsl.ContainerOp = kaniko(
            context_artifact=download_src_op.outputs['folder'],
            destination=f'{image_registry}/{name}',
            dockerfile=dockerfile,
        )
        # CPU request/limit can be more flexible (request < limit), because being assigned to a node
        # with insufficient CPU resource will only slow the task down, but not fail.
        task.container.set_cpu_request('1').set_cpu_limit('2')
        # Memory request/limit needs to be more rigid (request == limit), because in a node without
        # enough memory, the task can hang indefinetely or OOM.
        task.container.set_memory_request('4Gi').set_memory_limit('4Gi')
        task.set_display_name(f'build-image-{name}')
        task.set_retry(
            1, policy='Always'
        )  # Always -> retry on both system error and user code failure.
        return task

    # build v2 compatible image
    build_kfp_launcher_op = build_image(
        name='kfp-launcher',
        dockerfile='v2/container/launcher/Dockerfile',
    )
    # build sample test image
    build_samples_image_op = build_image(
        name='v2-sample-test',
        dockerfile='v2/test/Dockerfile',
    )
    # build v2 engine images
    build_kfp_launcher_v2_op = build_image(
        name='kfp-launcher-v2',
        dockerfile='v2/container/launcher-v2/Dockerfile'
    )
    build_kfp_driver_op = build_image(
        name='kfp-driver', dockerfile='v2/container/driver/Dockerfile'
    )

    # run test samples in parallel
    with kfp.dsl.ParallelFor(samples_config) as sample:
        run_sample_op: kfp.dsl.ContainerOp = run_sample(
            name=sample.name,
            sample_path=sample.path,
            gcs_root=gcs_root,
            external_host=kfp_host,
            launcher_image=build_kfp_launcher_op.outputs['digest'],
            launcher_v2_image=build_kfp_launcher_v2_op.outputs['digest'],
            driver_image=build_kfp_driver_op.outputs['digest'],
        )
        run_sample_op.container.image = build_samples_image_op.outputs['digest']
        run_sample_op.set_display_name(f'sample_{sample.name}')
        run_sample_op.set_retry(1, policy='Always')


def main(
    context: str,
    host: str,
    gcr_root: str,
    gcs_root: str,
    experiment: str = 'v2_sample_test'
):
    client = kfp.Client(host=host)
    client.create_experiment(
        name=experiment,
        description='An experiment with Kubeflow Pipelines v2 sample test runs.'
    )
    run_result = client.create_run_from_pipeline_func(
        v2_sample_test,
        {
            'context': context,
            'image_registry': f'{gcr_root}/test',
            'gcs_root': gcs_root,
            'kfp_host': host,
        },
        experiment_name=experiment,
    )
    print("Run details page URL:")
    print(f"{host}/#/runs/details/{run_result.run_id}")
    run_response = run_result.wait_for_run_completion(PIPELINE_TIME_OUT)
    run = run_response.run
    from pprint import pprint
    # Hide verbose content
    run_response.run.pipeline_spec.workflow_manifest = None
    pprint(run_response.run)
    print("Run details page URL:")
    print(f"{host}/#/runs/details/{run_result.run_id}")
    assert run.status == 'Succeeded'
    # TODO(Bobgy): print debug info


# %%
if __name__ == "__main__":
    import fire
    fire.Fire(main)
