import apiClient from './client'
import { insightsApi } from './insights'

export interface SubmitTinyTrainRequest {
  text: string
  name?: string
  maxSteps?: number
  maxSeconds?: number
}

export interface SubmitTinyTrainResponse {
  job_id: string
  status: string
  message: string
}

export const trainingApi = {
  /**
   * Submit a tiny-training run through the generic command endpoint.
   * The backend worker owns gating (tier + flags) and isolation;
   * denials surface as failed jobs carrying the reason.
   */
  submitTinyTrain: async (
    request: SubmitTinyTrainRequest
  ): Promise<SubmitTinyTrainResponse> => {
    if (!request.text || !request.text.trim()) {
      throw new Error('Training text must be non-empty')
    }
    const response = await apiClient.post<SubmitTinyTrainResponse>(
      '/commands/jobs',
      {
        command: 'train_tiny',
        app: 'open_notebook',
        input: {
          text: request.text,
          name: request.name ?? 'tiny-run',
          max_steps: request.maxSteps,
          max_seconds: request.maxSeconds,
        },
      }
    )
    return response.data
  },

  getCommandStatus: insightsApi.getCommandStatus,
  waitForCommand: insightsApi.waitForCommand,
}
