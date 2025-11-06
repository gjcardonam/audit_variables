pipeline {
  agent {
    docker {
      image 'python:3.11-slim'
      reuseNode true
    }
  }

  options { timestamps() }

  parameters {
    // Un solo config para todos los ambientes (local y CI)
    string(
      name: 'CONFIG',
      defaultValue: 'config.json',
      description: 'Ruta al JSON de configuración del auditor de templating'
    )
  }

  environment {
    PYTHONUNBUFFERED = '1'

    // En Jenkins (ambiente "prod"/CI) NO queremos JSON/CSV:
    WRITE_JSON = '0'
    WRITE_CSV  = '0'
  }

  stages {
    stage('Checkout') {
      steps {
        deleteDir()
        checkout scm
      }
    }

    stage('Deps') {
      steps {
        sh '''
          pip install --no-cache-dir --upgrade pip
          pip install --no-cache-dir requests psycopg2-binary python-dotenv
        '''
      }
    }

    stage('Run audit') {
      steps {
        withCredentials([
          usernamePassword(
            credentialsId: 'GRAFANA_CREDS',
            usernameVariable: 'GRAFANA_USERNAME',
            passwordVariable: 'GRAFANA_PASSWORD'
          ),
          usernamePassword(
            credentialsId: 'PG_CREDS',
            usernameVariable: 'DB_USER',
            passwordVariable: 'DB_PASSWORD'
          )
        ]) {
          sh '''
            echo "Usando config: ${CONFIG}"
            python templating_auditor.py -c ${CONFIG} | tee run.log
          '''
        }
      }
    }
  }

  post {
    always {
      archiveArtifacts artifacts: 'run.log', allowEmptyArchive: true
    }
  }
}
