import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './api'

// Events console (Phase 1 item 1.3). Reads/writes reuse the dashboard events router →
// shared examlops.data.events.outbox_stats / examlops.events.publish (pure platform.db). The
// NovaFabric transactional-outbox event backbone — enqueue + backlog depth only, no live relay.

export interface EventsStats {
  pending: number
  published: number
  poison: number
}

export interface EventsView {
  stats: EventsStats
  total: number
}

export interface PublishEventBody {
  topic: string
  payload?: string
}

export interface PublishEventResult {
  id: number
  topic: string
}

export const useEvents = () =>
  useQuery<EventsView>({
    queryKey: ['events'],
    queryFn: () => apiFetch<EventsView>('/api/v1/events'),
  })

export const publishEvent = (body: PublishEventBody) =>
  apiFetch<PublishEventResult>('/api/v1/events', {
    method: 'POST',
    body: JSON.stringify(body),
  })

export const usePublishEvent = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: PublishEventBody) => publishEvent(body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['events'] }),
  })
}
