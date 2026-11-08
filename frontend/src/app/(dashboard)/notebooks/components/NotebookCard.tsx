'use client'

import { useRouter } from 'next/navigation'
import { NotebookResponse } from '@/lib/types/api'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { MoreHorizontal, Archive, ArchiveRestore, Trash2, FileText, StickyNote } from 'lucide-react'
import { formatDistanceToNow } from 'date-fns'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { useUpdateNotebook } from '@/lib/hooks/use-notebooks'
import { NotebookDeleteDialog } from './NotebookDeleteDialog'
import { useState } from 'react'
import { useTranslation } from '@/lib/hooks/use-translation'
import { getDateLocale } from '@/lib/utils/date-locale'
import { cn } from '@/lib/utils'
import { ProgressRing } from '@/components/ui/progress-ring'
import { getFieldColor, getFieldLabel } from '@/lib/field-colors'
interface NotebookCardProps {
  notebook: NotebookResponse
}

export function NotebookCard({ notebook }: NotebookCardProps) {
  const { t, language } = useTranslation()
  const [showDeleteDialog, setShowDeleteDialog] = useState(false)
  const router = useRouter()
  const updateNotebook = useUpdateNotebook()

  const handleArchiveToggle = (e: React.MouseEvent) => {
    e.stopPropagation()
    updateNotebook.mutate({
      id: notebook.id,
      data: { archived: !notebook.archived }
    })
  }

  const handleCardClick = () => {
    router.push(`/notebooks/${encodeURIComponent(notebook.id)}`)
  }

  return (
    <>
      <Card 
        className="group card-hover"
        onClick={handleCardClick}
        style={{ cursor: 'pointer' }}
      >
          <CardHeader className="pb-3">
            <div className="flex items-start justify-between">
              <div className="flex-1 min-w-0">
                <span aria-hidden className="mb-2 block h-2 w-2 rounded-[3px] bg-teal" />
                <CardTitle className="text-base truncate">
                  {notebook.name}
                </CardTitle>
                {notebook.archived && (
                  <Badge variant="secondary" className="mt-1">
                    {t('notebooks.archived')}
                  </Badge>
                )}
                {notebook.field && (
                  <Badge
                    className={cn(
                      "mt-1",
                      getFieldColor(notebook.field).badge
                    )}
                  >
                    {getFieldLabel(notebook.field)}
                  </Badge>
                )}
                {!notebook.field && (
                  <Badge variant="outline" className="mt-1 text-muted-foreground">
                    {t('settings.fieldUnused')}
                  </Badge>
                )}
              </div>
              
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button
                    variant="ghost"
                    size="sm"
                    className="opacity-0 group-hover:opacity-100 transition-opacity"
                    onClick={(e) => e.stopPropagation()}
                  >
                    <MoreHorizontal className="h-4 w-4" />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" onClick={(e) => e.stopPropagation()}>
                  <DropdownMenuItem onClick={handleArchiveToggle}>
                    {notebook.archived ? (
                      <>
                        <ArchiveRestore className="h-4 w-4 mr-2" />
                        {t('notebooks.unarchive')}
                      </>
                    ) : (
                      <>
                        <Archive className="h-4 w-4 mr-2" />
                        {t('notebooks.archive')}
                      </>
                    )}
                  </DropdownMenuItem>
                  <DropdownMenuItem
                    onClick={(e) => {
                      e.stopPropagation()
                      setShowDeleteDialog(true)
                    }}
                    className="text-destructive"
                  >
                    <Trash2 className="h-4 w-4 mr-2" />
                    {t('common.delete')}
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
            </div>
          </CardHeader>
          
          <CardContent>
            <CardDescription className="line-clamp-2 text-sm">
              {notebook.description || t('chat.noDescription')}
            </CardDescription>

            <div className="mt-3 text-xs text-muted-foreground">
              {t('common.updated', { time: formatDistanceToNow(new Date(notebook.updated), { 
                addSuffix: true,
                locale: getDateLocale(language)
              }) })}
            </div>

            {/* Item counts footer */}
            <div className="mt-3 flex items-center gap-3 border-t pt-3 text-xs text-muted-foreground">
              <span className="flex items-center gap-1">
                <FileText className="h-3 w-3" />
                <span>{notebook.source_count}</span>
              </span>
              <span className="flex items-center gap-1">
                <StickyNote className="h-3 w-3" />
                <span>{notebook.note_count}</span>
              </span>
              {notebook.expected_completion_duration && (
                <div className="ml-2 flex items-center gap-1">
                  <ProgressRing
                    value={
                      Math.min(
                        100,
                        Math.round(
                          (notebook.source_count /
                            notebook.expected_completion_duration) *
                            100
                        )
                      )
                    }
                    size={16}
                    strokeWidth={2}
                    showLabel={false}
                  />
                </div>
              )}
            </div>
          </CardContent>
      </Card>

      <NotebookDeleteDialog
        open={showDeleteDialog}
        onOpenChange={setShowDeleteDialog}
        notebookId={notebook.id}
        notebookName={notebook.name}
      />
    </>
  )
}
