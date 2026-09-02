{
    _id: 'rstudio',
    name: 'Rstudio Tool',
    title: 'Rstudio Session',
    short_description: 'Interactive RStudio environment for data analysis in OpenVRE',
    long_description: 'Launch an interactive RStudio session within the OpenVRE workspace to analyze, visualize, and explore your data. The containerized environment provides a full RStudio IDE with access to your files, supporting reproducible workflows and custom R packages.',
    url: 'https://rstudio.com/',
    publication: '',
    owner: {
      author: 'Maria Paola Ferri',
      institution: 'Barcelona Supercomputing Center',
      contact: 'maria.ferri@bsc.es',
      url: ''
    },
    status: 1,
    external: true,
    keywords: [ 'rstudio' ],
    infrastructure: {
      memory: 12,
      cpus: 1,
      interactive: true,
      executable: '',
      container_image: 'rstudio',
      container_port: '8787',
      clouds: { local: { launcher: 'docker_SGE', default_cloud: true } },
      volumes: {
        '/repository': '/home/rstudio/repository:ro',
        '/rstudio_data': '/home/rstudio/workspace'
      }
    },
    input_files: [
      {
        name: 'input_fasta',
        description: 'Input FASTA file containing sequences.',
        help: 'Provide the FASTA file from which to extract sequences.',
        format: [ 'FASTA' ],
        data_type: [ 'sequence_data' ],
        required: true,
        allow_multiple: false
      }
    ],
    input_files_public_dir: [],
    input_files_combinations: [ { description: 'Start an RStudio session', input_files: [] } ],
    arguments: [],
    has_custom_viewer: false,
    output_files: [
      {
        name: 'output',
        required: true,
        allow_multiple: false,
        file: {
          format: 'FASTA',
          data_type: 'extracted_sequences',
          meta_data: { visible: true, description: '', tool: 'rstudio' },
          file_path: 'output'
        }
      }
    ],
    sites: [ { site_id: 'local', status: 1 } ],
    input_files_combinations_internal: [ [] ]
}
