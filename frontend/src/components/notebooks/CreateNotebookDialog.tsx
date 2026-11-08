'use client'

import { useEffect } from 'react'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useCreateNotebook } from '@/lib/hooks/use-notebooks'
import { useTranslation } from '@/lib/hooks/use-translation'


const FIELD_OPTIONS = [
  { value: 'art', label: 'Art' },
  { value: 'music', label: 'Music' },
  { value: 'biology', label: 'Biology' },
  { value: 'astronomy', label: 'Astronomy' },
  { value: 'geography', label: 'Geography' },
  { value: 'general', label: 'General' },
  { value: 'cultural', label: 'Cultural' },
  { value: 'custom', label: 'Custom' },
]

const createNotebookSchema = z.object({
  name: z.string().min(1, 'Name is required'),
  description: z.string().optional(),
  field: z.string().optional(),
  expected_completion_duration: z.number().min(1).optional(),
  curated: z.boolean().optional(),
})

type CreateNotebookFormData = z.infer<typeof createNotebookSchema>

interface CreateNotebookDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
}

export function CreateNotebookDialog({ open, onOpenChange }: CreateNotebookDialogProps) {
  const { t } = useTranslation()
  const createNotebook = useCreateNotebook()
  const {
    register,
    handleSubmit,
    formState: { errors, isValid },
    reset,
  } = useForm<CreateNotebookFormData>({
    resolver: zodResolver(createNotebookSchema),
    mode: 'onChange',
    defaultValues: {
      name: '',
      description: '',
    },
  })

  const closeDialog = () => onOpenChange(false)

  const onSubmit = async (data: CreateNotebookFormData) => {
    await createNotebook.mutateAsync(data)
    closeDialog()
    reset()
  }

  useEffect(() => {
    if (!open) {
      reset()
    }
  }, [open, reset])

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-[480px]">
        <DialogHeader>
          <DialogTitle>{t('notebooks.createNew')}</DialogTitle>
          <DialogDescription>
            {t('notebooks.createNewDesc')}
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit(onSubmit)} className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="notebook-name">{t('common.name')} *</Label>
            <Input
              id="notebook-name"
              {...register('name')}
              placeholder={t('notebooks.namePlaceholder')}
              autoComplete="off"
            />
            {errors.name && (
              <p className="text-sm text-destructive">{errors.name.message}</p>
            )}
          </div>

          <div className="space-y-2">
            <Label htmlFor="notebook-description">{t('common.description')}</Label>
            <Textarea
              id="notebook-description"
              {...register('description')}
              placeholder={t('notebooks.descPlaceholder')}
              rows={4}
            />
          </div>

          <div className="space-y-2">
            <Label htmlFor="notebook-field">{t('settings.fieldOfStudy')}</Label>
            <Select
              {...register('field')}
              defaultValue=""
            >
              <SelectTrigger id="notebook-field-select" className="w-full">
                <SelectValue placeholder={t('settings.fieldPlaceholder')} />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="">— {t('settings.fieldNone')} —</SelectItem>
                {FIELD_OPTIONS.map((option) => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-2">
            <Label htmlFor="notebook-duration">{t('settings.expectedDuration')}</Label>
            <Input
              id="notebook-duration"
              type="number"
              {...register('expected_completion_duration')}
              placeholder={t('settings.durationPlaceholder')}
            />
          </div>

          <div className="flex items-center gap-2">
            <input
              id="notebook-curated"
              type="checkbox"
              {...register('curated')}
              defaultChecked={false}
              className="h-4 w-4 shrink-0 rounded border-input bg-background text-primary"
            />
            <Label htmlFor="notebook-curated" className="text-sm">
              {t('settings.curatedCourse')}
            </Label>
          </div>

          <DialogFooter className="gap-2 sm:gap-0">
            <Button type="button" variant="outline" onClick={closeDialog}>
              {t('common.cancel')}
            </Button>
            <Button type="submit" disabled={!isValid || createNotebook.isPending}>
              {createNotebook.isPending ? t('common.creating') : t('notebooks.createNew')}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
